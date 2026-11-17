"""命令行：serve / export / reply / rebuild / list。

用法（在 tools/paper-review/ 下，或用 ./review 包装脚本在任意目录）：
  python -m paper_review serve   --paper example --port 8790
  python -m paper_review export  --paper example [--round N|latest|current|all] [--status open|...|all]
  python -m paper_review reply   --paper example --id 3 --status addressed --reply "已改"
  python -m paper_review reply   --paper example --batch replies.json
  python -m paper_review rebuild --paper example [--base zh-en-sync/6] [--supp]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import config as configmod
from .store import Store, StoreError


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="配置文件（默认项目目录中的 config.toml）")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="批注库与缓存的根目录（默认各论文的 paper-session/review/；测试与演示用）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper_review", description="中英双语论文审阅工具",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动本机审阅服务（只监听 127.0.0.1）")
    add_common(serve)
    serve.add_argument("--paper", required=True, help="默认打开的论文（编号、目录名或路径，须在白名单内）")
    serve.add_argument("--port", type=int, default=None, help="端口（默认 config.toml 的 server.port）")
    serve.add_argument("--host", default=None, help="监听地址，只能是 127.0.0.1 / localhost / ::1")
    serve.add_argument("--next-free", action="store_true", help="端口被占用时依次尝试后续端口")

    export = sub.add_parser("export", help="导出批注：JSON + 按段落分组的 Markdown 摘要")
    add_common(export)
    export.add_argument("--paper", required=True)
    export.add_argument("--round", default="latest", help="轮次：整数、latest（默认，最近提交的一轮）、current、all")
    export.add_argument("--status", default="all", help="open / addressed / declined / all（默认）")
    export.add_argument("--out", type=Path, default=None, help="输出目录（默认 <state>/exports/）")
    export.add_argument("--print", dest="print_format", choices=("md", "json", "none"), default="none",
                        help="同时把 Markdown 或 JSON 打到标准输出")
    export.add_argument("--no-locations", action="store_true", help="不渲染稿件（导出不带段落位置与摘录）")

    reply = sub.add_parser("reply", help="写回处理状态与回复（单条或批量 JSON）")
    add_common(reply)
    reply.add_argument("--paper", required=True)
    reply.add_argument("--id", type=int, help="批注编号")
    reply.add_argument("--status", choices=("open", "addressed", "declined"))
    reply.add_argument("--reply", default=None, help="回复内容（显示在网页批注下方）")
    reply.add_argument("--batch", type=Path, help='批量文件：[{"id": 3, "status": "addressed", "reply": "…"}, …]')

    rebuild = sub.add_parser("rebuild", help="重新解析与渲染（稿件修改后），打印统计并通知运行中的服务")
    add_common(rebuild)
    rebuild.add_argument("--paper", required=True)
    rebuild.add_argument("--base", default=None, help="对比基准 ref（默认：本稿上一个同步 tag）")
    rebuild.add_argument("--supp", action="store_true", help="包含补充材料")
    rebuild.add_argument("--json", action="store_true", help="以 JSON 输出统计")
    rebuild.add_argument("--no-previews", action="store_true", help="不预先生成缺失的图片预览")

    listing = sub.add_parser("list", help="列出白名单中的论文")
    add_common(listing)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = configmod.load(args.config)
        if args.command == "list":
            for paper in cfg.papers:
                print(f"{paper.id}\t{paper.name}\t{paper.manuscript}")
            return 0
        paper = cfg.paper(args.paper)
        if args.command == "serve":
            return serve(cfg, paper, args)
        if args.command == "export":
            return export(cfg, paper, args)
        if args.command == "reply":
            return reply(cfg, paper, args)
        if args.command == "rebuild":
            return rebuild(cfg, paper, args)
    except (KeyError, ValueError, StoreError, FileNotFoundError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"错误：{message}", file=sys.stderr)
        return 2
    return 0


def open_store(paper, state_dir: Path | None) -> Store:
    return Store(paper.state_dir(state_dir) / "annotations.sqlite", paper.name)


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def serve(cfg, paper, args) -> int:
    import uvicorn

    from .server import check_host, create_app, server_config, write_server_file

    host = check_host(args.host or cfg.host)
    port = args.port or cfg.port
    if not port_free(host, port):
        if not args.next_free:
            print(f"错误：端口 {port} 已被占用（可加 --next-free 自动顺延）", file=sys.stderr)
            return 2
        for candidate in range(port + 1, port + 21):
            if port_free(host, candidate):
                port = candidate
                break
        else:
            print("错误：找不到空闲端口", file=sys.stderr)
            return 2
    app = create_app(cfg, paper.id, args.state_dir)
    server_files = [p.state_dir(args.state_dir) / "server.json" for p in cfg.papers]
    for path in server_files:
        write_server_file(path, host, port)
    shown = f"[{host}]" if ":" in host else host
    print(f"论文审阅服务：http://{shown}:{port}/  （默认论文 {paper.name}）", flush=True)
    print(f"远程查看：ssh -L {port}:127.0.0.1:{port} <本机主机名>，再在浏览器打开 http://127.0.0.1:{port}/", flush=True)
    try:
        uvicorn.Server(server_config(app, host, port)).run()
    finally:
        for path in server_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("port") == port:
                    path.unlink()
            except (OSError, ValueError):
                pass
    return 0


def build_doc(cfg, paper, state_dir: Path | None, base: str | None = None, supp: bool = False):
    from .document import PaperState

    state = PaperState(paper, cfg.repo, paper.state_dir(state_dir))
    return state, state.get(base_ref=base, include_supp=supp)


def export(cfg, paper, args) -> int:
    from .exporting import export as do_export
    from .exporting import write_export

    store = open_store(paper, args.state_dir)
    doc = None
    if not args.no_locations:
        try:
            _, built = build_doc(cfg, paper, args.state_dir)
            doc = built.doc
        except Exception as exc:  # noqa: BLE001 - 稿件渲染失败不影响导出批注本身
            print(f"警告：稿件渲染失败，导出不含段落位置：{exc}", file=sys.stderr)
    payload, md = do_export(store, doc, args.round, args.status)
    out_dir = args.out or paper.state_dir(args.state_dir) / "exports"
    json_path, md_path = write_export(payload, md, out_dir)
    if args.print_format == "md":
        print(md)
    elif args.print_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"count": payload["count"], "round": payload["round"], "status": payload["status"],
                          "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    return 0


def reply(cfg, paper, args) -> int:
    from .exporting import load_batch

    store = open_store(paper, args.state_dir)
    if args.batch:
        results = store.reply_many(load_batch(args.batch))
        print(json.dumps({"updated": [r["id"] for r in results]}, ensure_ascii=False))
        return 0
    if args.id is None or args.status is None:
        print("错误：单条回复需要 --id 与 --status（或用 --batch 文件）", file=sys.stderr)
        return 2
    result = store.reply(args.id, args.status, args.reply)
    print(json.dumps({"id": result["id"], "status": result["status"], "reply": result["reply"]}, ensure_ascii=False))
    return 0


def rebuild(cfg, paper, args) -> int:
    from .server import summary

    state, built = build_doc(cfg, paper, args.state_dir, args.base, args.supp)
    previews = []
    if not args.no_previews:
        for fid in list(state.figures.items):
            path = state.figures.preview(fid)
            previews.append({"id": fid, "file": str(state.figures.items[fid].source), "preview": str(path) if path else None})
    state_dir = paper.state_dir(args.state_dir)
    cache = state_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "document.json").write_text(json.dumps(built.doc, ensure_ascii=False), encoding="utf-8")
    stats = built.stats
    (cache / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    notified = notify_server(state_dir, paper.id, args.base, args.supp)
    if args.json:
        print(json.dumps({"summary": summary(stats), "stats": stats, "previews": previews, "notified": notified,
                          "base": built.doc["header"]["base"]}, ensure_ascii=False, indent=2, default=str))
        return 0
    base = built.doc["header"]["base"]
    print(f"{paper.name}：中文 {stats['zh_blocks']} 段，英文 {stats['en_blocks']} 段，对齐 {stats['aligned']}，"
          f"仅中文 {len(stats['zh_only'])}，仅英文 {len(stats['en_only'])}")
    for lang in ("zh", "en"):
        r = stats["render"][lang]
        print(f"  [{lang}] 未识别宏 {r['unknown_macros']} {r['unknown_macro_names'] or ''}；未解析引用 {len(r['unresolved_refs'])}；"
              f"未解析引文 {len(r['unresolved_cites'])}；按源码推算的标签 {len(r['refs_from_source'])}；"
              f"编号与源码不一致 {len(r['ref_mismatch'])}；表格降级为源码 {r['table_fallbacks']}")
    print(f"  对比基准 {base['ref']}（{base['short']}）：本轮修改 {stats['changed']} 处，删除 {stats['deleted']} 段"
          + (f"；{base['note']}" if base.get("note") else ""))
    sync = stats["sync_check"]
    print(f"  同步检查：{'可用' if sync['ok'] else '不可用'}，退出码 {sync['exit']}，告警 {dict(sync['warnings'])}")
    if previews:
        missing = [p for p in previews if not p["preview"]]
        print(f"  图片预览 {len(previews) - len(missing)}/{len(previews)}")
    print(f"  快照：{cache / 'document.json'}；" + ("已通知运行中的服务" if notified else "未发现运行中的服务"))
    return 0


def notify_server(state_dir: Path, pid: str, base: str | None, supp: bool) -> bool:
    path = state_dir / "server.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    host = info.get("host", "127.0.0.1")
    host = f"[{host}]" if ":" in host else host
    query = []
    if base:
        query.append("base=" + urllib.request.quote(base, safe=""))
    if supp:
        query.append("supp=1")
    url = f"http://{host}:{info.get('port')}/api/papers/{pid}/rebuild" + ("?" + "&".join(query) if query else "")
    request = urllib.request.Request(url, method="POST", data=b"{}",
                                     headers={"X-Paper-Review": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False
