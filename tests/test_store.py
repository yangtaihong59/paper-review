"""批注库：增删改查、轮次提交与锁定、已读标记、导出（JSON + Markdown）与回复往返。"""

from __future__ import annotations

import json

import pytest

from paper_review import cli
from paper_review.exporting import export, load_batch, markdown
from paper_review.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "annotations.sqlite", "demo-paper")


def test_crud_and_validation(store):
    note = store.create(para_id="res-01", lang="en", category="措辞", quote="six of eight", text="改成 6 of 8")
    assert note["id"] == 1 and note["status"] == "open" and note["round"] == 1 and note["paper"] == "demo-paper"
    updated = store.update(note["id"], text="改成 “6 of 8”", category="数字")
    assert updated["text"] == "改成 “6 of 8”" and updated["category"] == "数字"
    assert updated["updated_at"] >= note["updated_at"]
    assert [n["id"] for n in store.list(para_id="res-01")] == [1]
    with pytest.raises(StoreError):
        store.create(para_id="res-01", lang="fr", category="措辞", text="x")
    with pytest.raises(StoreError):
        store.create(para_id="res-01", lang="zh", category="风格", text="x")
    with pytest.raises(StoreError):
        store.create(para_id="res-01", lang="zh", category="措辞", text="   ")
    store.delete(note["id"])
    assert store.list() == []
    with pytest.raises(StoreError) as exc:
        store.get(1)
    assert exc.value.status == 404


def test_new_notes_need_only_text(store):
    """新批注只有文字（和可选引文）：语言记为 auto，类别记为“其他”；Markdown 摘要不写这两项。"""
    note = store.create(para_id="res-03", quote="收益递减", text="这句可以删掉")
    assert note["lang"] == "auto" and note["category"] == "其他"
    legacy = store.create(para_id="res-03", lang="en", category="数字", text="旧式批注")
    payload, md = export(store, None, "current", "all")
    lines = {n["id"]: line for n in payload["annotations"] for line in md.splitlines() if line.startswith(f"- **#{n['id']}**")}
    assert "类别" not in lines[note["id"]] and "针对" not in lines[note["id"]]
    assert "类别 数字" in lines[legacy["id"]] and "针对英文" in lines[legacy["id"]]
    assert {"lang", "category", "quote", "text"} <= set(payload["annotations"][0])  # JSON 字段保持兼容


def test_old_database_is_migrated_in_place(tmp_path):
    """旧库的 lang 约束不含 auto：打开时先备份再重建表，原有批注一条不少。"""
    import sqlite3

    path = tmp_path / "annotations.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, paper TEXT NOT NULL, para_id TEXT NOT NULL,
            lang TEXT NOT NULL CHECK (lang IN ('zh', 'en', 'both')),
            category TEXT NOT NULL CHECK (category IN ('措辞', '内容', '数字', '图表', '引用', '删改', '其他')),
            quote TEXT, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', reply TEXT, round INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'user', zh_sha TEXT,
            en_sha TEXT, replied_at TEXT);
        INSERT INTO annotations (paper, para_id, lang, category, quote, text, round, created_at, updated_at)
        VALUES ('demo-paper', 'intro-01', 'zh', '措辞', '引文', '旧批注', 1, '2026-11-05T00:00:00+00:00',
                '2026-11-05T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    store = Store(path, "demo-paper")
    old = store.list()
    assert len(old) == 1 and old[0]["text"] == "旧批注" and old[0]["lang"] == "zh" and old[0]["category"] == "措辞"
    assert store.create(para_id="intro-01", text="新批注")["lang"] == "auto"
    assert list(tmp_path.glob("annotations.backup-*.sqlite")), "迁移前应有备份"
    Store(path, "demo-paper")  # 再次打开不再迁移
    assert len(list(tmp_path.glob("annotations.backup-*.sqlite"))) == 1


def test_rounds_submit_and_lock(store):
    a = store.create(para_id="intro-01", lang="zh", category="内容", text="第一条")
    submitted = store.submit_round(commit="abc123", sync_tag="zh-en-sync/7", note="先处理数字")
    assert submitted["round"] == 1 and submitted["submitted_at"] and submitted["count"] == 1
    with pytest.raises(StoreError) as exc:
        store.update(a["id"], text="改")
    assert exc.value.status == 409
    with pytest.raises(StoreError):
        store.delete(a["id"])
    b = store.create(para_id="intro-02", lang="both", category="其他", text="第二轮")
    assert b["round"] == 2
    with pytest.raises(StoreError):
        # 空轮不能提交
        Store(store.path, "other-paper").submit_round()
    rounds = store.rounds()
    assert [r["round"] for r in rounds] == [1, 2] and rounds[0]["count"] == 1 and rounds[1]["submitted_at"] is None


def test_reads(store):
    store.set_read("res-01", True, "aaaa1111", "bbbb2222")
    assert store.reads()["res-01"]["zh_sha"] == "aaaa1111"
    store.set_read("res-01", False)
    assert "res-01" not in store.reads()


def test_export_reply_round_trip(store, tmp_path):
    n1 = store.create(para_id="res-02", lang="en", category="数字", quote="13.77", text="与表中不一致", en_sha="1234abcd")
    n2 = store.create(para_id="intro-01", lang="zh", category="措辞", text="句子太长\n建议拆成两句")
    n3 = store.create(para_id="res-02", lang="zh", category="图表", text="表头单位")
    store.submit_round(commit="c0ffee", sync_tag="zh-en-sync/7")
    doc = {
        "header": {"commit": {"short": "c0ffee"}, "sync": {"tag": "zh-en-sync/7"}},
        "rows": [
            {"type": "heading", "key": "h:section:intro-01", "level": "section", "id": None,
             "zh": {"number": "I", "text": "引言"}, "en": {"number": "I", "text": "Introduction"}},
            {"type": "block", "id": "intro-01", "zh": {"file": "zh/sections/01.tex", "line": 4, "html": "<p>引言段</p>", "sha": "z1"},
             "en": {"file": "sections/01.tex", "line": 4, "html": "<p>Intro</p>", "sha": "e1"}},
            {"type": "block", "id": "res-02", "zh": {"file": "zh/sections/05.tex", "line": 30, "html": "<p>表</p>", "sha": "z2"},
             "en": {"file": "sections/05.tex", "line": 31, "html": "<p>Table</p>", "sha": "e2-changed"}},
        ],
    }
    payload, md = export(store, doc, "latest", "open")
    assert payload["round"] == 1 and payload["count"] == 3 and payload["status"] == "open"
    # 按文档顺序分组：intro-01 在 res-02 之前。
    assert [a["para_id"] for a in payload["annotations"]] == ["intro-01", "res-02", "res-02"]
    first_res = next(a for a in payload["annotations"] if a["id"] == n1["id"])
    assert first_res["location"]["en"] == {"file": "sections/05.tex", "line": 31}
    assert first_res["changed_since_note"] == {"en": True}
    assert "## intro-01 · I 引言" in md and "## res-02" in md
    assert md.index("## intro-01") < md.index("## res-02")
    assert "  > 13.77" in md and "建议拆成两句" in md
    # 单条回复与批量回复（原子：任何一条不合法则全部不写）。
    store.reply(n1["id"], "addressed", "已与表 III 核对，改为 13.77")
    batch = tmp_path / "replies.json"
    batch.write_text(json.dumps([{"id": n2["id"], "status": "declined", "reply": "保留原句"},
                                 {"id": n3["id"], "status": "bogus"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(StoreError):
        store.reply_many(load_batch(batch))
    assert store.get(n2["id"])["status"] == "open"
    batch.write_text(json.dumps({"replies": [{"id": n2["id"], "status": "declined", "reply": "保留原句"},
                                             {"id": n3["id"], "status": "addressed"}]}, ensure_ascii=False), encoding="utf-8")
    store.reply_many(load_batch(batch))
    payload, md = export(store, doc, 1, "all")
    statuses = {a["id"]: (a["status"], a["reply"]) for a in payload["annotations"]}
    assert statuses[n1["id"]] == ("addressed", "已与表 III 核对，改为 13.77")
    assert statuses[n2["id"]] == ("declined", "保留原句")
    assert statuses[n3["id"]][0] == "addressed"
    assert "回复（已处理" in md and "保留原句" in md
    assert export(store, doc, "latest", "open")[0]["count"] == 0
    assert markdown(export(store, None, "all", None)[0]).startswith("# 批注汇总")


def test_cli_export_and_reply(cfg, tmp_path, capsys):
    """命令行：reply 写回、export 输出 JSON 与 Markdown 文件（使用临时状态目录，不碰真实批注库）。"""
    paper = cfg.paper("example")
    store = Store(paper.state_dir(tmp_path) / "annotations.sqlite", paper.name)
    note = store.create(para_id="intro-01", lang="en", category="措辞", quote="beats", text="用 outperforms")
    assert cli.main(["reply", "--paper", "example", "--state-dir", str(tmp_path), "--id", str(note["id"]),
                     "--status", "addressed", "--reply", "已改"]) == 0
    capsys.readouterr()
    assert cli.main(["export", "--paper", "example", "--state-dir", str(tmp_path), "--round", "all",
                     "--status", "addressed", "--out", str(tmp_path / "exports")]) == 0
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert info["count"] == 1
    data = json.loads(open(info["json"], encoding="utf-8").read())
    assert data["annotations"][0]["reply"] == "已改"
    assert data["annotations"][0]["location"]["en"]["file"].startswith("sections/")
    text = open(info["markdown"], encoding="utf-8").read()
    assert "## intro-01" in text and "> beats" in text
    assert cli.main(["reply", "--paper", "/etc", "--state-dir", str(tmp_path), "--id", "1", "--status", "open"]) == 2
