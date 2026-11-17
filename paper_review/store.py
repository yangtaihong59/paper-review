"""批注库：<论文>/paper-session/review/annotations.sqlite（目录被 */paper-session/ 忽略，不入库）。

表：annotations（批注与统筹回复）、reads（逐段已读标记，记下当时的段落指纹）、rounds（批注轮次与提交时间）。
已提交轮次的批注在网页端只读；统筹用命令行 reply 写回状态与回复。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ("措辞", "内容", "数字", "图表", "引用", "删改", "其他")
# 新批注不再选语言与类别：lang 记为 auto、category 记为“其他”；旧批注保留原值。
LANGS = ("zh", "en", "both", "auto")
DEFAULT_LANG = "auto"
DEFAULT_CATEGORY = "其他"
STATUSES = ("open", "addressed", "declined")
MAX_TEXT = 20000
MAX_QUOTE = 5000

ANNOTATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper TEXT NOT NULL,
    para_id TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'auto' CHECK (lang IN ('zh', 'en', 'both', 'auto')),
    category TEXT NOT NULL DEFAULT '其他' CHECK (category IN ('措辞', '内容', '数字', '图表', '引用', '删改', '其他')),
    quote TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'addressed', 'declined')),
    reply TEXT,
    round INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'user',
    zh_sha TEXT,
    en_sha TEXT,
    replied_at TEXT
);
"""
COLUMNS = ("id, paper, para_id, lang, category, quote, text, status, reply, round, created_at, updated_at, author, "
           "zh_sha, en_sha, replied_at")

SCHEMA = ANNOTATIONS_TABLE + """
CREATE INDEX IF NOT EXISTS annotations_para ON annotations (paper, para_id);
CREATE INDEX IF NOT EXISTS annotations_round ON annotations (paper, round, status);
CREATE TABLE IF NOT EXISTS reads (
    paper TEXT NOT NULL,
    para_id TEXT NOT NULL,
    zh_sha TEXT,
    en_sha TEXT,
    read_at TEXT NOT NULL,
    PRIMARY KEY (paper, para_id)
);
CREATE TABLE IF NOT EXISTS rounds (
    paper TEXT NOT NULL,
    round INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    submitted_at TEXT,
    commit_sha TEXT,
    sync_tag TEXT,
    note TEXT,
    PRIMARY KEY (paper, round)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class StoreError(ValueError):
    """请求不合法（字段取值、已提交轮次的修改等）；服务端映射为 4xx。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, paper: str):
        self.path = path
        self.paper = paper
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', 'paper_review.annotations.v1')")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库的 lang 约束不含 auto：先备份，再在一个事务内重建表（数据原样保留）。"""
        def old_schema() -> bool:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'annotations'").fetchone()
            return bool(row) and "'auto'" not in row[0]

        if not old_schema():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.stem}.backup-{stamp}.sqlite")
        dest = sqlite3.connect(backup)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if old_schema():  # 其他进程可能已经迁移过
                conn.execute("ALTER TABLE annotations RENAME TO annotations_old")
                conn.execute(ANNOTATIONS_TABLE)
                conn.execute(f"INSERT INTO annotations ({COLUMNS}) SELECT {COLUMNS} FROM annotations_old")
                conn.execute("DROP TABLE annotations_old")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # -- 轮次 ----------------------------------------------------------------
    def _current_round(self, conn: sqlite3.Connection) -> dict:
        row = conn.execute(
            "SELECT * FROM rounds WHERE paper = ? AND submitted_at IS NULL ORDER BY round DESC LIMIT 1", (self.paper,)
        ).fetchone()
        if row is not None:
            return dict(row)
        last = conn.execute("SELECT MAX(round) FROM rounds WHERE paper = ?", (self.paper,)).fetchone()[0] or 0
        conn.execute(
            "INSERT INTO rounds (paper, round, started_at) VALUES (?, ?, ?)", (self.paper, last + 1, now())
        )
        return dict(conn.execute("SELECT * FROM rounds WHERE paper = ? AND round = ?", (self.paper, last + 1)).fetchone())

    def current_round(self) -> dict:
        with self.transaction() as conn:
            return self._current_round(conn)

    def rounds(self) -> list[dict]:
        with self.transaction() as conn:
            self._current_round(conn)
            rows = conn.execute(
                """SELECT r.*, (SELECT COUNT(*) FROM annotations a WHERE a.paper = r.paper AND a.round = r.round) AS count,
                          (SELECT COUNT(*) FROM annotations a WHERE a.paper = r.paper AND a.round = r.round
                             AND a.status = 'open') AS open_count
                   FROM rounds r WHERE r.paper = ? ORDER BY r.round""",
                (self.paper,),
            ).fetchall()
            return [dict(row) for row in rows]

    def submit_round(self, commit: str | None = None, sync_tag: str | None = None, note: str | None = None) -> dict:
        with self.transaction() as conn:
            current = self._current_round(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE paper = ? AND round = ?", (self.paper, current["round"])
            ).fetchone()[0]
            if count == 0:
                raise StoreError(f"第 {current['round']} 轮还没有批注，无需提交", 409)
            conn.execute(
                "UPDATE rounds SET submitted_at = ?, commit_sha = ?, sync_tag = ?, note = ? WHERE paper = ? AND round = ?",
                (now(), commit, sync_tag, note, self.paper, current["round"]),
            )
            submitted = dict(
                conn.execute("SELECT * FROM rounds WHERE paper = ? AND round = ?", (self.paper, current["round"])).fetchone()
            )
            submitted["count"] = count
            return submitted

    def round_submitted(self, conn: sqlite3.Connection, number: int) -> bool:
        row = conn.execute("SELECT submitted_at FROM rounds WHERE paper = ? AND round = ?", (self.paper, number)).fetchone()
        return bool(row and row["submitted_at"])

    def latest_submitted(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(round) FROM rounds WHERE paper = ? AND submitted_at IS NOT NULL", (self.paper,)
            ).fetchone()
            return row[0]

    # -- 批注 ----------------------------------------------------------------
    @staticmethod
    def _validate(fields: dict, partial: bool = False) -> dict:
        clean = {}
        if "para_id" in fields or not partial:
            pid = str(fields.get("para_id") or "").strip()
            if not pid or len(pid) > 200:
                raise StoreError("para_id 不能为空，且不超过 200 字符")
            clean["para_id"] = pid
        if "lang" in fields or not partial:
            lang = fields.get("lang") or DEFAULT_LANG
            if lang not in LANGS:
                raise StoreError(f"lang 必须是 {'/'.join(LANGS)}")
            clean["lang"] = lang
        if "category" in fields or not partial:
            category = fields.get("category") or DEFAULT_CATEGORY
            if category not in CATEGORIES:
                raise StoreError(f"category 必须是 {'/'.join(CATEGORIES)}")
            clean["category"] = category
        if "text" in fields or not partial:
            text = str(fields.get("text") or "").strip()
            if not text:
                raise StoreError("批注内容不能为空")
            if len(text) > MAX_TEXT:
                raise StoreError(f"批注内容超过 {MAX_TEXT} 字符")
            clean["text"] = text
        if "quote" in fields:
            quote = fields.get("quote")
            quote = str(quote).strip() if quote is not None else None
            if quote and len(quote) > MAX_QUOTE:
                raise StoreError(f"引文超过 {MAX_QUOTE} 字符")
            clean["quote"] = quote or None
        for key in ("zh_sha", "en_sha"):
            if key in fields:
                value = fields.get(key)
                clean[key] = str(value)[:16] if value else None
        return clean

    def create(self, **fields) -> dict:
        clean = self._validate(fields)
        with self.transaction() as conn:
            current = self._current_round(conn)
            stamp = now()
            cur = conn.execute(
                """INSERT INTO annotations (paper, para_id, lang, category, quote, text, status, round, created_at,
                                            updated_at, author, zh_sha, en_sha)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
                (self.paper, clean["para_id"], clean["lang"], clean["category"], clean.get("quote"), clean["text"],
                 current["round"], stamp, stamp, str(fields.get("author") or "user")[:40], clean.get("zh_sha"),
                 clean.get("en_sha")),
            )
            return self._get(conn, cur.lastrowid)

    def _get(self, conn: sqlite3.Connection, annotation_id: int) -> dict:
        row = conn.execute("SELECT * FROM annotations WHERE paper = ? AND id = ?", (self.paper, annotation_id)).fetchone()
        if row is None:
            raise StoreError(f"批注 #{annotation_id} 不存在", 404)
        return dict(row)

    def get(self, annotation_id: int) -> dict:
        with self.connect() as conn:
            return self._get(conn, annotation_id)

    def list(self, round: int | None = None, status: str | None = None, para_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM annotations WHERE paper = ?"
        args: list = [self.paper]
        if round is not None:
            sql += " AND round = ?"
            args.append(round)
        if status:
            if status not in STATUSES:
                raise StoreError(f"status 必须是 {'/'.join(STATUSES)}")
            sql += " AND status = ?"
            args.append(status)
        if para_id:
            sql += " AND para_id = ?"
            args.append(para_id)
        sql += " ORDER BY id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def update(self, annotation_id: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("lang", "category", "quote", "text")}
        if not allowed:
            raise StoreError("没有可修改的字段（lang/category/quote/text）")
        clean = self._validate(allowed, partial=True)
        with self.transaction() as conn:
            current = self._get(conn, annotation_id)
            if self.round_submitted(conn, current["round"]):
                raise StoreError(f"第 {current['round']} 轮已提交，批注 #{annotation_id} 不能再修改；可另加新批注", 409)
            sets = ", ".join(f"{key} = ?" for key in clean)
            conn.execute(
                f"UPDATE annotations SET {sets}, updated_at = ? WHERE paper = ? AND id = ?",
                (*clean.values(), now(), self.paper, annotation_id),
            )
            return self._get(conn, annotation_id)

    def delete(self, annotation_id: int) -> None:
        with self.transaction() as conn:
            current = self._get(conn, annotation_id)
            if self.round_submitted(conn, current["round"]):
                raise StoreError(f"第 {current['round']} 轮已提交，批注 #{annotation_id} 不能删除", 409)
            conn.execute("DELETE FROM annotations WHERE paper = ? AND id = ?", (self.paper, annotation_id))

    def reply(self, annotation_id: int, status: str, reply: str | None) -> dict:
        if status not in STATUSES:
            raise StoreError(f"status 必须是 {'/'.join(STATUSES)}")
        with self.transaction() as conn:
            self._get(conn, annotation_id)
            stamp = now()
            conn.execute(
                "UPDATE annotations SET status = ?, reply = ?, replied_at = ?, updated_at = ? WHERE paper = ? AND id = ?",
                (status, (reply or "").strip() or None, stamp, stamp, self.paper, annotation_id),
            )
            return self._get(conn, annotation_id)

    def reply_many(self, items: list[dict]) -> list[dict]:
        """批量回复：先全部校验，再在一个事务内写入（全有或全无）。"""
        parsed = []
        for index, item in enumerate(items):
            try:
                annotation_id = int(item["id"])
            except (KeyError, TypeError, ValueError):
                raise StoreError(f"第 {index + 1} 条缺少整数 id") from None
            status = item.get("status")
            if status not in STATUSES:
                raise StoreError(f"第 {index + 1} 条（#{annotation_id}）的 status 必须是 {'/'.join(STATUSES)}")
            parsed.append((annotation_id, status, item.get("reply")))
        results = []
        with self.transaction() as conn:
            for annotation_id, status, reply in parsed:
                self._get(conn, annotation_id)
            stamp = now()
            for annotation_id, status, reply in parsed:
                conn.execute(
                    "UPDATE annotations SET status = ?, reply = ?, replied_at = ?, updated_at = ? "
                    "WHERE paper = ? AND id = ?",
                    (status, (str(reply).strip() if reply else None) or None, stamp, stamp, self.paper, annotation_id),
                )
                results.append(self._get(conn, annotation_id))
        return results

    # -- 已读 ----------------------------------------------------------------
    def reads(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM reads WHERE paper = ?", (self.paper,)).fetchall()
            return {row["para_id"]: {"zh_sha": row["zh_sha"], "en_sha": row["en_sha"], "read_at": row["read_at"]}
                    for row in rows}

    def set_read(self, para_id: str, read: bool, zh_sha: str | None = None, en_sha: str | None = None) -> None:
        para_id = str(para_id).strip()
        if not para_id or len(para_id) > 200:
            raise StoreError("para_id 不合法")
        with self.transaction() as conn:
            if read:
                conn.execute(
                    "INSERT INTO reads (paper, para_id, zh_sha, en_sha, read_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (paper, para_id) DO UPDATE SET zh_sha = excluded.zh_sha, en_sha = excluded.en_sha, "
                    "read_at = excluded.read_at",
                    (self.paper, para_id, (zh_sha or None), (en_sha or None), now()),
                )
            else:
                conn.execute("DELETE FROM reads WHERE paper = ? AND para_id = ?", (self.paper, para_id))
