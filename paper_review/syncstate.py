"""调用 zh_en_sync.py check --json 得到逐段同步状态（只读）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

STATE_OF = {
    "missing_en_id": "zh-only",
    "extra_en_id": "en-only",
    "zh_drift": "en-pending",
    "en_drift": "en-drift",
    "invalid_en_only": "en-drift",
}
# 同一段有多条告警时，状态取最需要处理的一条。
PRIORITY = ["zh-only", "en-only", "en-pending", "en-drift", "aligned"]


def run_check(tool: Path, manuscript: Path, timeout: int = 120) -> dict:
    """返回 {"ok": bool, "result": check JSON 或 None, "error": str, "exit": int}。退出码 1 也是有效结果。"""
    if not tool.is_file():
        return {"ok": False, "result": None, "error": f"找不到同步工具：{tool}", "exit": None}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(tool), "check", "--root", str(manuscript), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(manuscript),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "result": None, "error": str(exc), "exit": None}
    if proc.returncode not in (0, 1):
        return {"ok": False, "result": None, "error": (proc.stderr or proc.stdout).strip()[:500], "exit": proc.returncode}
    try:
        result = json.loads(proc.stdout)
    except ValueError as exc:
        return {"ok": False, "result": None, "error": f"check 输出不是 JSON：{exc}", "exit": proc.returncode}
    return {"ok": True, "result": result, "error": "", "exit": proc.returncode}


def states(check: dict) -> tuple[dict[str, dict], list[dict]]:
    """把 check 告警按段落编号归类：{id: {"state": ..., "warnings": [...]}}，以及无编号的全局告警。"""
    per_id: dict[str, dict] = {}
    global_warnings: list[dict] = []
    if not check or not check.get("ok"):
        return per_id, global_warnings
    for warning in check["result"].get("warnings", []):
        pid = warning.get("id")
        if not pid:
            global_warnings.append(warning)
            continue
        entry = per_id.setdefault(pid, {"state": "aligned", "warnings": []})
        state = STATE_OF.get(warning.get("code"))
        if state and PRIORITY.index(state) < PRIORITY.index(entry["state"]):
            entry["state"] = state
        if not state:
            entry["warnings"].append(warning)
        else:
            entry.setdefault("codes", []).append(warning.get("code"))
    return per_id, global_warnings
