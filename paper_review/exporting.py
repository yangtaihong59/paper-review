"""导出批注（JSON + 按段落分组的 Markdown 摘要）与批量回复文件的读取。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import STATUSES, Store, StoreError

LANG_NAME = {"zh": "中文", "en": "英文", "both": "中英", "auto": "自动"}
STATUS_NAME = {"open": "待处理", "addressed": "已处理", "declined": "不采纳"}
UTC8 = timezone(timedelta(hours=8))


def local_time(stamp: str | None) -> str:
    """UTC ISO 时间 → 新加坡时间（UTC+8）的可读形式。"""
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return moment.astimezone(UTC8).strftime("%Y-%m-%d %H:%M")


def resolve_round(store: Store, selector: str | int | None) -> int | None:
    """--round：整数、latest（默认，最近提交的一轮；尚未提交过时为当前轮）、current 或 all（返回 None）。"""
    if selector is None or selector == "latest":
        latest = store.latest_submitted()
        return latest if latest is not None else store.current_round()["round"]
    if selector == "current":
        return store.current_round()["round"]
    if selector == "all":
        return None
    try:
        return int(selector)
    except (TypeError, ValueError):
        raise StoreError(f"--round 取值不合法：{selector}") from None


def row_context(doc: dict | None) -> tuple[dict, list[str]]:
    """段落编号 → 位置、所在章节与摘录；以及文档顺序。"""
    context: dict[str, dict] = {}
    order: list[str] = []
    if not doc:
        return context, order
    section = ""
    for row in doc.get("rows", []):
        if row["type"] == "heading":
            cell = row.get("zh") or row.get("en") or {}
            if row.get("level") in ("section", "subsection", "subsubsection"):
                section = f"{cell.get('number', '')} {cell.get('text', '')}".strip()
            pid = row.get("id") or row["key"]
        else:
            pid = row.get("id")
        if not pid:
            continue
        info = {"section": section, "location": {}, "excerpt": {}, "sha": {}}
        for lang in ("zh", "en"):
            cell = row.get(lang)
            if not cell:
                continue
            if cell.get("file"):
                info["location"][lang] = {"file": cell["file"], "line": cell.get("line")}
            text = re.sub(r"<[^>]+>", "", cell.get("html", ""))
            text = re.sub(r"\s+", " ", text).strip()
            info["excerpt"][lang] = text[:140] + ("…" if len(text) > 140 else "")
            info["sha"][lang] = cell.get("sha")
        context[pid] = info
        order.append(pid)
    return context, order


def export(store: Store, doc: dict | None, round_selector=None, status: str | None = None) -> tuple[dict, str]:
    if status in (None, "", "all"):
        status = None
    elif status not in STATUSES:
        raise StoreError(f"--status 必须是 {'/'.join(STATUSES)} 或 all")
    number = resolve_round(store, round_selector)
    notes = store.list(round=number, status=status)
    context, order = row_context(doc)
    rank = {pid: i for i, pid in enumerate(order)}
    notes.sort(key=lambda n: (rank.get(n["para_id"], len(rank)), n["id"]))
    header = (doc or {}).get("header", {})
    records = []
    for note in notes:
        info = context.get(note["para_id"], {})
        changed = {}
        for lang in ("zh", "en"):
            then = note.get(f"{lang}_sha")
            current = info.get("sha", {}).get(lang)
            if then and current:
                changed[lang] = then != current
        records.append({**note, "section": info.get("section", ""), "location": info.get("location", {}),
                        "excerpt": info.get("excerpt", {}), "changed_since_note": changed})
    rounds = {r["round"]: r for r in store.rounds()}
    payload = {
        "schema": "paper_review.export.v1",
        "paper": store.paper,
        "round": number if number is not None else "all",
        "round_info": rounds.get(number) if number is not None else None,
        "status": status or "all",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": (header.get("commit") or {}).get("short"),
        "sync_tag": (header.get("sync") or {}).get("tag"),
        "count": len(records),
        "annotations": records,
    }
    return payload, markdown(payload)


def markdown(payload: dict) -> str:
    round_text = f"第 {payload['round']} 轮" if payload["round"] != "all" else "全部轮次"
    status_text = STATUS_NAME.get(payload["status"], "全部状态")
    lines = [
        f"# 批注汇总：{payload['paper']}，{round_text}，{status_text}（{payload['count']} 条）",
        "",
        f"导出时间 {local_time(payload['exported_at'])}（UTC+8）；稿件提交 `{payload.get('commit') or '?'}`；"
        f"同步点 `{payload.get('sync_tag') or '无'}`。",
    ]
    info = payload.get("round_info") or {}
    if info.get("submitted_at"):
        lines.append(f"本轮提交于 {local_time(info['submitted_at'])}（UTC+8）。")
    elif payload["round"] != "all":
        lines.append("本轮尚未提交（用户可能仍在批注）。")
    lines += ["", "回复：`python -m paper_review reply --paper <论文> --id <编号> --status addressed|declined --reply \"…\"`。", ""]
    if not payload["annotations"]:
        lines += ["（没有符合条件的批注。）", ""]
        return "\n".join(lines)
    current = None
    for note in payload["annotations"]:
        if note["para_id"] != current:
            current = note["para_id"]
            section = f" · {note['section']}" if note.get("section") else ""
            lines += ["", f"## {current}{section}", ""]
            loc = note.get("location") or {}
            parts = []
            for lang in ("zh", "en"):
                if lang in loc:
                    parts.append(f"{LANG_NAME[lang]} `{loc[lang]['file']}:{loc[lang]['line']}`")
            if parts:
                lines.append("位置：" + " · ".join(parts))
                lines.append("")
        flags = []
        changed = note.get("changed_since_note") or {}
        if any(changed.values()):
            flags.append("批注后段落已改：" + "、".join(LANG_NAME[k] for k, v in changed.items() if v))
        meta = f"**#{note['id']}** {STATUS_NAME.get(note['status'], note['status'])} · 第 {note['round']} 轮 · " \
               f"{local_time(note['created_at'])}"
        # 语言与类别只有旧批注里真正选过时才写出（新批注记为 auto / 其他）。
        if note.get("category") and note["category"] != "其他":
            meta += f" · 类别 {note['category']}"
        if note.get("lang") and note["lang"] != "auto":
            meta += f" · 针对{LANG_NAME.get(note['lang'], note['lang'])}"
        lines.append(f"- {meta}" + (f"（{'；'.join(flags)}）" if flags else ""))
        if note.get("quote"):
            for quote_line in note["quote"].splitlines() or [""]:
                lines.append(f"  > {quote_line}")
        for text_line in note["text"].splitlines():
            lines.append(f"  {text_line}" if text_line.strip() else "")
        if note.get("reply"):
            lines.append(f"  - 回复（{STATUS_NAME.get(note['status'], note['status'])}，{local_time(note.get('replied_at'))}）：{note['reply']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_export(payload: dict, md: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC8).strftime("%Y%m%d-%H%M%S")
    round_part = f"r{payload['round']}" if payload["round"] != "all" else "all"
    base = out_dir / f"annotations-{round_part}-{payload['status']}-{stamp}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


def load_batch(path: Path) -> list[dict]:
    """批量回复文件：[{"id": 3, "status": "addressed", "reply": "…"}, …] 或 {"replies": [...]}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("replies", data.get("annotations"))
    if not isinstance(data, list):
        raise StoreError("批量回复文件必须是列表，或含 replies 列表的对象")
    return data
