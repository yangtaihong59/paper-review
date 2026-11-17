"""插图：按 \\graphicspath 找到图文件，提供 PNG 预览（论文自带的 figures/previews/*.png，
缺失或比 PDF 旧时用 pdftoppm 生成，缓存在 <论文>/paper-session/review/previews/）。

服务端只按登记过的编号提供图片，不接受任意路径。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS = ("", ".pdf", ".png", ".jpg", ".jpeg")
PREVIEW_DPI = 200
STALE_TOLERANCE = 120.0  # 秒；预览比 PDF 旧超过这个量才视为过期


@dataclass
class FigureRef:
    id: str
    name: str
    source: Path
    url: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "url": self.url, "file": self.source.name}


class FigureRegistry:
    def __init__(self, paper_id: str, paper_dir: Path, manuscript: Path, cache_dir: Path,
                 graphicspaths: dict[str, list[str]]):
        self.paper_id = paper_id
        self.paper_dir = paper_dir.resolve()
        self.manuscript = manuscript.resolve()
        self.cache_dir = cache_dir
        self.graphicspaths = graphicspaths
        self.items: dict[str, FigureRef] = {}
        self.lock = threading.Lock()

    def register(self, lang: str, name: str) -> FigureRef | None:
        base = self.manuscript / "zh" if lang == "zh" else self.manuscript
        dirs = [*self.graphicspaths.get(lang, []), ""]
        for directory in dirs:
            for ext in EXTENSIONS:
                candidate = (base / directory / (name + ext)).resolve()
                if candidate.is_file() and candidate.is_relative_to(self.paper_dir):
                    return self._add(name, candidate)
        # 中文稿可能没有自己的 graphicspath：退回英文稿的。
        if lang == "zh":
            return self.register("en", name)
        return None

    def _add(self, name: str, path: Path) -> FigureRef:
        fid = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
        try:
            version = int(path.stat().st_mtime)
        except OSError:
            version = 0
        ref = FigureRef(fid, name, path, f"api/papers/{self.paper_id}/figures/{fid}.png?v={version}")
        self.items[fid] = ref
        return ref

    def preview(self, fid: str) -> Path | None:
        """返回可直接发送的 PNG/JPG 路径；需要时生成。"""
        ref = self.items.get(fid)
        if ref is None:
            return None
        source = ref.source
        if source.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return source
        if source.suffix.lower() != ".pdf":
            return None
        bundled = source.parent / "previews" / (source.stem + ".png")
        try:
            pdf_mtime = source.stat().st_mtime
        except OSError:
            return None
        if bundled.is_file() and bundled.stat().st_mtime + STALE_TOLERANCE >= pdf_mtime:
            return bundled
        return self.generate(source)

    def generate(self, source: Path) -> Path | None:
        stat = source.stat()
        digest = hashlib.sha1(f"{source}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:10]
        target = self.cache_dir / "previews" / f"{source.stem}-{digest}.png"
        if target.is_file():
            return target
        if shutil.which("pdftoppm") is None:
            return None
        with self.lock:
            if target.is_file():
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            stem = target.with_suffix("")
            try:
                subprocess.run(
                    ["pdftoppm", "-png", "-r", str(PREVIEW_DPI), "-f", "1", "-l", "1", "-singlefile",
                     str(source), str(stem)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                return None
            # 清理同一图的旧版本缓存。
            for old in target.parent.glob(f"{source.stem}-*.png"):
                if old != target:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        return target if target.is_file() else None

    def status(self) -> list[dict]:
        rows = []
        for ref in self.items.values():
            source = ref.source
            bundled = source.parent / "previews" / (source.stem + ".png")
            kind = "image"
            if source.suffix.lower() == ".pdf":
                if bundled.is_file() and bundled.stat().st_mtime + STALE_TOLERANCE >= source.stat().st_mtime:
                    kind = "bundled-preview"
                else:
                    kind = "generated-preview"
            rows.append({"id": ref.id, "file": str(source), "preview": kind})
        return rows
