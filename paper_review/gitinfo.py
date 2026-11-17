"""Git 只读查询（参数列表调用，不经 shell）。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} 失败")
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve(repo: Path, ref: str) -> str | None:
    if not ref or ref.startswith("-") or re.search(r"\s", ref):
        return None
    out = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", check=False)
    return out or None


def head(repo: Path) -> dict:
    line = git(repo, "log", "-1", "--format=%H%x09%h%x09%cI%x09%s", check=False)
    parts = line.split("\t", 3) if line else ["", "", "", ""]
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return {"commit": parts[0], "short": parts[1], "date": parts[2], "subject": parts[3] if len(parts) > 3 else "",
            "branch": branch}


def dirty(repo: Path, prefix: str) -> dict:
    """稿件目录下未提交的改动：按中文 zh/ 与其余（英文、共享）分开计数。"""
    out = git(repo, "status", "--porcelain", "--untracked-files=no", "--", prefix, check=False)
    zh, en = [], []
    for line in out.splitlines():
        path = line[3:].strip()
        rel = path[len(prefix) :].lstrip("/") if path.startswith(prefix) else path
        (zh if rel.startswith("zh/") else en).append(rel)
    return {"zh": zh, "en": en}


def sync_tags_for(manuscript: Path) -> list[str]:
    """本稿 SYNC.md / SYNC.json 记录过的同步 tag（tag 编号全仓共享，不能只看 tag 名）。"""
    tags: list[str] = []
    history = manuscript / "SYNC.md"
    if history.is_file():
        tags += re.findall(r"^##\s+(zh-en-sync/\d+)\s*$", history.read_text(encoding="utf-8"), re.M)
    state = manuscript / "SYNC.json"
    if state.is_file():
        try:
            tag = json.loads(state.read_text(encoding="utf-8")).get("tag")
        except ValueError:
            tag = None
        if isinstance(tag, str) and tag not in tags:
            tags.append(tag)
    return sorted(set(tags), key=lambda t: int(t.rsplit("/", 1)[1]))


def ref_options(repo: Path, manuscript_prefix: str, manuscript: Path, paper_dir_name: str) -> list[dict]:
    """供“对比基准”下拉框选择的 ref：本稿同步 tag、HEAD、近期涉及稿件的提交、其他相关 tag。"""
    options: list[dict] = []
    tags = sync_tags_for(manuscript)
    present = [tag for tag in tags if resolve(repo, tag)]
    for tag in reversed(tags):
        commit = resolve(repo, tag)
        note = ""
        if commit and present and tag == present[-1]:
            note = "（最近同步点）"
        elif commit and len(present) >= 2 and tag == present[-2]:
            note = "（本轮修订前）"
        options.append({"ref": tag, "label": tag + (note if commit else "（本地不存在）"), "group": "同步点",
                        "available": bool(commit)})
    options.append({"ref": "HEAD", "label": "HEAD（最新提交）", "group": "提交", "available": True})
    log = git(repo, "log", "-n", "15", "--format=%h%x09%cs%x09%s", "--", manuscript_prefix, check=False)
    for line in log.splitlines():
        short, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        options.append({"ref": short, "label": f"{short} {date} {subject[:48]}", "group": "提交", "available": True})
    for tag in git(repo, "tag", "--list", "--sort=-creatordate", check=False).splitlines():
        if tag.startswith("zh-en-sync/"):
            continue
        if paper_dir_name in tag or tag.split("/")[0] in paper_dir_name or "submit" in tag:
            options.append({"ref": tag, "label": tag, "group": "其他 tag", "available": True})
    return options
