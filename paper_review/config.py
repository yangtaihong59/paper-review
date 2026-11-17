"""读取 config.toml：论文白名单、同步工具路径与服务端口。

服务端和命令行只接受白名单内的论文目录；--paper 可以写编号、目录名或路径，解析后必须命中白名单。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = TOOL_DIR / "config.toml"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass
class PaperConfig:
    id: str
    dir: Path
    manuscript: Path
    sync_tool: Path
    build_dir: str = "build"
    title: str = ""
    default_base: str | None = None

    @property
    def name(self) -> str:
        return self.dir.name

    def state_dir(self, override: Path | None = None) -> Path:
        """批注库与缓存的位置：默认 <论文>/paper-session/review/（被 */paper-session/ 忽略）。"""
        if override is not None:
            return override / self.id
        return self.dir / "paper-session" / "review"


@dataclass
class Config:
    repo: Path
    papers: list[PaperConfig]
    host: str = "127.0.0.1"
    port: int = 8790
    path: Path = DEFAULT_CONFIG
    extra: dict = field(default_factory=dict)

    def paper(self, key: str) -> PaperConfig:
        """按编号、目录名或路径查找白名单中的论文；找不到时抛 KeyError。"""
        key = key.strip()
        for paper in self.papers:
            if key in (paper.id, paper.name):
                return paper
        candidate = Path(key)
        options = [candidate] if candidate.is_absolute() else [Path.cwd() / candidate, self.repo / candidate]
        for option in options:
            try:
                resolved = option.resolve()
            except OSError:
                continue
            for paper in self.papers:
                if resolved in (paper.dir, paper.manuscript):
                    return paper
        allowed = ", ".join(f"{p.id} ({p.name})" for p in self.papers)
        raise KeyError(f"论文不在白名单中：{key}；可选：{allowed}")


def load(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    repo = (path.parent / data.get("repo", "../..")).resolve()
    server = data.get("server", {})
    defaults = data.get("defaults", {})
    sync_default = defaults.get("sync_tool")
    papers = []
    for item in data.get("papers", []):
        paper_dir = (repo / item["dir"]).resolve()
        if not paper_dir.is_relative_to(repo):
            raise ValueError(f"论文目录必须在仓库内：{item['dir']}")
        manuscript = (paper_dir / item.get("manuscript", "manuscript-ieee")).resolve()
        if not manuscript.is_relative_to(paper_dir):
            raise ValueError(f"稿件目录必须在论文目录内：{item}")
        sync_path = item.get("sync_tool", sync_default)
        sync_tool = (repo / sync_path).resolve() if sync_path else TOOL_DIR / "scripts" / "zh_en_sync.py"
        papers.append(
            PaperConfig(
                id=item.get("id", paper_dir.name),
                dir=paper_dir,
                manuscript=manuscript,
                sync_tool=sync_tool,
                build_dir=item.get("build_dir", "build"),
                title=item.get("title", ""),
                default_base=item.get("default_base") or None,
            )
        )
    host = server.get("host", "127.0.0.1")
    if host not in LOOPBACK:
        raise ValueError(f"config.toml 的 server.host 只能是本机回环地址：{host}")
    return Config(repo=repo, papers=papers, host=host, port=int(server.get("port", 8790)), path=path, extra=data)
