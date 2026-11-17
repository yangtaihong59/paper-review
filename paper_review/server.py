"""本机审阅服务（FastAPI）。

安全边界：
- 只监听回环地址（127.0.0.1 / localhost / ::1），远程查看用 SSH 端口转发；
- 校验 Host 头只能是回环名，防 DNS 重绑定；写操作要求自定义请求头 X-Paper-Review，防跨站请求；
- 论文只来自 config.toml 白名单；图片只按登记过的编号提供；不接受任意文件路径；
- 稿件源文件、构建产物、Git 只读；写入只发生在批注库（以及 paper-session/review/ 下的预览缓存）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import gitinfo
from .config import LOOPBACK, Config, PaperConfig
from .document import PaperState, manuscript_prefix
from .store import Store, StoreError

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
CSRF_HEADER = "x-paper-review"
HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?$", re.I)


class Registry:
    """每篇论文的渲染缓存与批注库（惰性创建）。"""

    def __init__(self, cfg: Config, state_root: Path | None):
        self.cfg = cfg
        self.state_root = state_root
        self.states: dict[str, PaperState] = {}
        self.stores: dict[str, Store] = {}
        self.lock = threading.Lock()

    def paper(self, pid: str) -> PaperConfig:
        for paper in self.cfg.papers:
            if paper.id == pid:
                return paper
        raise HTTPException(404, f"论文不在白名单中：{pid}")

    def state(self, pid: str) -> PaperState:
        paper = self.paper(pid)
        with self.lock:
            if pid not in self.states:
                self.states[pid] = PaperState(paper, self.cfg.repo, paper.state_dir(self.state_root))
            return self.states[pid]

    def store(self, pid: str) -> Store:
        paper = self.paper(pid)
        with self.lock:
            if pid not in self.stores:
                self.stores[pid] = Store(paper.state_dir(self.state_root) / "annotations.sqlite", paper.name)
            return self.stores[pid]


def create_app(cfg: Config, default_paper: str | None = None, state_root: Path | None = None) -> FastAPI:
    app = FastAPI(title="paper-review", docs_url=None, redoc_url=None, openapi_url=None)
    registry = Registry(cfg, state_root)
    app.state.registry = registry
    default_id = default_paper or (cfg.papers[0].id if cfg.papers else None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not HOST_RE.match(host):
            return JSONResponse({"detail": "只接受本机回环地址的请求（请通过 SSH 端口转发访问）"}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "缺少请求头 X-Paper-Review: 1"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def store_error(exc: StoreError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/papers")
    def papers() -> dict:
        return {
            "default": default_id,
            "papers": [{"id": p.id, "dir": p.name} for p in cfg.papers],
        }

    @app.get("/api/papers/{pid}/document")
    def document(pid: str, base: str | None = None, supp: int = 0, refresh: int = 0) -> Response:
        state = registry.state(pid)
        if base and (len(base) > 200 or base.startswith("-")):
            raise HTTPException(400, "基准 ref 不合法")
        built = state.get(base_ref=base or None, include_supp=bool(supp), force=bool(refresh))
        doc = dict(built.doc)
        doc["signature"] = built.signature
        doc["round"] = registry.store(pid).current_round()
        body = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        return Response(body, media_type="application/json")

    @app.get("/api/papers/{pid}/version")
    def version(pid: str, base: str | None = None, supp: int = 0) -> dict:
        state = registry.state(pid)
        return {"signature": state.current_signature(base or None, bool(supp))}

    @app.post("/api/papers/{pid}/rebuild")
    def rebuild(pid: str, base: str | None = None, supp: int = 0) -> dict:
        state = registry.state(pid)
        built = state.get(base_ref=base or None, include_supp=bool(supp), force=True)
        return {"signature": built.signature, "stats": summary(built.stats)}

    @app.get("/api/papers/{pid}/refs")
    def refs(pid: str) -> dict:
        paper = registry.paper(pid)
        return {"options": gitinfo.ref_options(cfg.repo, manuscript_prefix(paper, cfg.repo), paper.manuscript, paper.name)}

    @app.get("/api/papers/{pid}/figures/{fid}.png")
    def figure(pid: str, fid: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{16}", fid):
            raise HTTPException(404, "图片编号不合法")
        state = registry.state(pid)
        path = state.figures.preview(fid)
        if path is None or not path.is_file():
            raise HTTPException(404, "没有这张图的预览")
        media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})

    # -- 批注 ----------------------------------------------------------------
    @app.get("/api/papers/{pid}/annotations")
    def list_annotations(pid: str, round: int | None = None, status: str | None = None) -> dict:
        store = registry.store(pid)
        try:
            return {"annotations": store.list(round=round, status=status or None), "rounds": store.rounds()}
        except StoreError as exc:
            raise store_error(exc) from None

    @app.post("/api/papers/{pid}/annotations")
    async def create_annotation(pid: str, request: Request) -> dict:
        store = registry.store(pid)
        payload = await json_body(request)
        try:
            return store.create(**{k: payload.get(k) for k in ("para_id", "lang", "category", "quote", "text",
                                                                "zh_sha", "en_sha") if k in payload})
        except StoreError as exc:
            raise store_error(exc) from None

    @app.patch("/api/papers/{pid}/annotations/{aid}")
    async def update_annotation(pid: str, aid: int, request: Request) -> dict:
        store = registry.store(pid)
        payload = await json_body(request)
        try:
            return store.update(aid, **payload)
        except StoreError as exc:
            raise store_error(exc) from None

    @app.delete("/api/papers/{pid}/annotations/{aid}")
    def delete_annotation(pid: str, aid: int) -> dict:
        store = registry.store(pid)
        try:
            store.delete(aid)
        except StoreError as exc:
            raise store_error(exc) from None
        return {"deleted": aid}

    @app.get("/api/papers/{pid}/reads")
    def reads(pid: str) -> dict:
        return {"reads": registry.store(pid).reads()}

    @app.put("/api/papers/{pid}/reads")
    async def set_read(pid: str, request: Request) -> dict:
        store = registry.store(pid)
        payload = await json_body(request)
        items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
        try:
            for item in items[:2000]:
                store.set_read(str(item.get("para_id", "")), bool(item.get("read", True)), item.get("zh_sha"),
                               item.get("en_sha"))
        except StoreError as exc:
            raise store_error(exc) from None
        return {"reads": store.reads()}

    @app.get("/api/papers/{pid}/rounds")
    def rounds(pid: str) -> dict:
        return {"rounds": registry.store(pid).rounds()}

    @app.post("/api/papers/{pid}/rounds/submit")
    async def submit(pid: str, request: Request) -> dict:
        store = registry.store(pid)
        paper = registry.paper(pid)
        payload = await json_body(request)
        head = gitinfo.head(cfg.repo)
        tag = None
        state_file = paper.manuscript / "SYNC.json"
        if state_file.is_file():
            try:
                tag = json.loads(state_file.read_text(encoding="utf-8")).get("tag")
            except ValueError:
                tag = None
        try:
            submitted = store.submit_round(commit=head.get("commit"), sync_tag=tag, note=str(payload.get("note") or "")[:2000] or None)
        except StoreError as exc:
            raise store_error(exc) from None
        return {"submitted": submitted, "current": store.current_round()}

    return app


async def json_body(request: Request) -> dict:
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "请求体必须是 JSON")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON 无法解析") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON 必须是对象")
    return payload


def summary(stats: dict) -> dict:
    return {
        "zh_blocks": stats["zh_blocks"],
        "en_blocks": stats["en_blocks"],
        "aligned": stats["aligned"],
        "zh_only": len(stats["zh_only"]),
        "en_only": len(stats["en_only"]),
        "unknown_macros": {lang: stats["render"][lang]["unknown_macros"] for lang in ("zh", "en")},
        "changed": stats["changed"],
    }


def check_host(host: str) -> str:
    if host not in LOOPBACK:
        raise ValueError(f"只允许监听本机回环地址（127.0.0.1 / localhost / ::1），拒绝：{host}")
    return host


def server_config(app: FastAPI, host: str, port: int):
    """构造 uvicorn 配置（单独成函数，便于测试校验监听地址）。"""
    import uvicorn

    return uvicorn.Config(app, host=check_host(host), port=port, log_level="info", access_log=False,
                          proxy_headers=False, server_header=False, timeout_graceful_shutdown=3)


def write_server_file(path: Path, host: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port}, ensure_ascii=False), encoding="utf-8")
