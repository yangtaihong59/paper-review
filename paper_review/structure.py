"""按 main.tex 的 \\input 顺序遍历一种语言的稿件，切出编号块、标题与跨块的环境上下文。

编号块边界与 zh_en_sync.py 相同：从编号行下一行到下一编号行（或文件末尾）之前，指纹也按同一规则计算。
渲染用的“内容”另行清理：去掉 \\input、\\maketitle、\\bibliography 等结构命令，把标题拆成独立条目，
并记录块开始时仍未闭合的环境（表格被拆成表题、表体、表注三块，列表项各自编号时都靠它渲染）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .texutil import (
    INPUT,
    LIST_ENVS,
    MARKER,
    VERBATIM_ENVS,
    TexError,
    find_env_end,
    fingerprint,
    match_brace,
    read_env_args,
    read_group,
    read_optional,
    skip_ws,
    uncomment,
)

HEADING_LEVELS = ("part", "section", "subsection", "subsubsection")
HEADING = re.compile(r"\\(part|section|subsection|subsubsection)(\*?)\s*(?=[\[{])")
STRUCTURAL = re.compile(
    r"\\(?:maketitle|clearpage|newpage|cleardoublepage|IEEEpeerreviewmaketitle|IEEEdisplaynontitleabstractindextext"
    r"|balance|onecolumn|twocolumn|appendix)\b\*?"
    r"|\\(?:bibliographystyle|bibliography|bstctlcite|nocite|IEEEtriggeratref|IEEEtriggercmd)\s*\{[^{}]*\}"
    r"|\\(?:begin|end)\s*\{document\}"
)


# ---------------------------------------------------------------------------
# 文件来源：工作区或某个 Git 提交
# ---------------------------------------------------------------------------


class FileProvider:
    """以稿件根目录（manuscript-ieee/）为基准读取文件，路径为 POSIX 相对路径。"""

    label = "worktree"

    def exists(self, rel: str) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def read(self, rel: str) -> str:  # pragma: no cover - 接口
        raise NotImplementedError

    def mtime(self, rel: str) -> float | None:
        return None


class WorktreeProvider(FileProvider):
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise TexError(f"路径越界：{rel}")
        return path

    def exists(self, rel: str) -> bool:
        try:
            return self._path(rel).is_file()
        except TexError:
            return False

    def read(self, rel: str) -> str:
        return self._path(rel).read_text(encoding="utf-8")

    def mtime(self, rel: str) -> float | None:
        try:
            return self._path(rel).stat().st_mtime
        except (OSError, TexError):
            return None


class GitProvider(FileProvider):
    """读取某个提交中的稿件文件（git ls-tree / git show；参数列表调用，不经 shell）。"""

    def __init__(self, repo: Path, commit: str, prefix: str):
        self.repo = repo
        self.commit = commit
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.label = commit
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit, "--", self.prefix or "."],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.files = {line.removeprefix(self.prefix) for line in out.splitlines() if line}
        self.cache: dict[str, str] = {}

    def exists(self, rel: str) -> bool:
        return str(PurePosixPath(rel)) in self.files

    def read(self, rel: str) -> str:
        rel = str(PurePosixPath(rel))
        if rel not in self.cache:
            if rel not in self.files:
                raise FileNotFoundError(rel)
            self.cache[rel] = subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{self.commit}:{self.prefix}{rel}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        return self.cache[rel]


def normalize_rel(path: str) -> str:
    """规范化相对路径（处理 ../ 与 ./），越出根目录时抛错。"""
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise TexError(f"路径越界：{path}")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------


@dataclass
class EnvEntry:
    name: str
    begin: str
    items: int = 0
    parts: int = 0

    def snapshot(self) -> "EnvEntry":
        return EnvEntry(self.name, self.begin, self.items, self.parts)


@dataclass
class Heading:
    level: str
    starred: bool
    title: str
    label: str | None
    file: str
    line: int
    block_id: str | None = None
    kind: str = "heading"


@dataclass
class Block:
    id: str
    file: str
    line: int
    raw: str
    content: str
    env: list[EnvEntry]
    metadata: dict[str, str] = field(default_factory=dict)
    unmarked: bool = False
    part: str = "main"
    kind: str = "block"

    @property
    def sha(self) -> str:
        return fingerprint(self.raw)


@dataclass
class LangWalk:
    lang: str
    items: list[Heading | Block]
    files: list[str]
    preamble: str
    warnings: list[dict]

    @property
    def blocks(self) -> list[Block]:
        return [item for item in self.items if isinstance(item, Block)]


# ---------------------------------------------------------------------------
# 遍历
# ---------------------------------------------------------------------------


class Walker:
    def __init__(self, provider: FileProvider, lang: str, lang_root: str, part: str = "main"):
        self.provider = provider
        self.lang = lang
        self.lang_root = lang_root.strip("/")
        self.part = part
        self.items: list[Heading | Block] = []
        self.stack: list[EnvEntry] = []
        self.visited: list[str] = []
        self.warnings: list[dict] = []
        self.preamble_parts: list[str] = []
        self.pseudo_counter: dict[str, int] = {}

    # 路径解析：语言根（zh/）优先，其次稿件根（manuscript-ieee/），与 zh/latexmkrc 的 TEXINPUTS 一致。
    def resolve(self, name: str, base_dir: str = "") -> str | None:
        name = name.strip()
        candidates = [name] if name.endswith(".tex") else [name + ".tex", name]
        roots = [self.lang_root, ""] if self.lang_root else [""]
        for root in roots:
            for candidate in candidates:
                try:
                    rel = normalize_rel(f"{root}/{candidate}" if root else candidate)
                except TexError:
                    continue
                if self.provider.exists(rel):
                    return rel
        return None

    def local(self, rel: str) -> str:
        """sync 工具记录的文件名：相对语言根。"""
        if self.lang_root and rel.startswith(self.lang_root + "/"):
            return rel[len(self.lang_root) + 1 :]
        return rel

    def is_structural(self, rel: str) -> bool:
        local = self.local(rel)
        if local.startswith(("sections/", "supp/")):
            return True
        if local.startswith("shared/"):
            return False
        try:
            return "%%¶" in self.provider.read(rel)
        except (OSError, FileNotFoundError, subprocess.CalledProcessError):
            return False

    def expand_shared_inputs(self, text: str, depth: int = 0) -> str:
        """块内的 \\input{shared/...} 就地展开（与 zh_en_sync.expand_shared 一致，只限 shared/）。"""
        if depth > 6:
            return text

        def replace(match: re.Match) -> str:
            rel = self.resolve(match[1])
            if rel is None or not self.local(rel).startswith("shared/"):
                return match[0]
            try:
                return self.expand_shared_inputs(uncomment(self.provider.read(rel)), depth + 1)
            except (OSError, FileNotFoundError, subprocess.CalledProcessError):
                return match[0]

        return INPUT.sub(replace, text)

    def walk(self, main: str = "main.tex") -> LangWalk:
        main_rel = self.resolve(main)
        if main_rel is None:
            raise FileNotFoundError(f"找不到 {self.lang_root}/{main}")
        self.process(main_rel, is_main=True)
        # 与 sync 工具一致：未被 \input 的 sections/*.tex 也纳入（附在末尾并告警）。
        return LangWalk(self.lang, self.items, self.visited, "\n".join(self.preamble_parts), self.warnings)

    def process(self, rel: str, is_main: bool = False) -> None:
        if rel in self.visited:
            return
        self.visited.append(rel)
        text = self.provider.read(rel)
        lines = text.splitlines(keepends=True)
        markers = {i: m for i, line in enumerate(lines) if (m := MARKER.match(line))}
        marker_lines = sorted(markers)
        raw_bodies = {}
        for pos, i in enumerate(marker_lines):
            end = marker_lines[pos + 1] if pos + 1 < len(marker_lines) else len(lines)
            raw_bodies[i] = "".join(lines[i + 1 : end])
        begin = 0
        if is_main:
            for idx, line in enumerate(lines):
                if "\\begin{document}" in uncomment(line):
                    begin = idx + 1
                    self.preamble_parts.append("".join(lines[:idx]))
                    break
            else:
                self.preamble_parts.append(text)
                return
        local = self.local(rel)
        segment: list[str] = []
        state = {"block": None, "start": begin + 1}

        def flush() -> None:
            self.flush(local, "".join(segment), state["block"], state["start"])
            segment.clear()

        for i in range(begin, len(lines)):
            line = lines[i]
            if i in markers:
                flush()
                marker = markers[i]
                metadata = {}
                for token in marker[2].split():
                    if "=" in token:
                        key, value = token.split("=", 1)
                        metadata[key] = value
                state["block"] = (marker[1], i + 1, raw_bodies[i], metadata)
                state["start"] = i + 2
                continue
            plain = uncomment(line)
            if is_main and "\\end{document}" in plain:
                break
            inputs = [m for m in INPUT.finditer(plain)]
            structural = []
            for m in inputs:
                target = self.resolve(m[1])
                if target and self.is_structural(target):
                    structural.append(target)
            if structural:
                flush()
                for target in structural:
                    self.process(target)
                # \input 之后、下一编号行之前的文字不属于该块的渲染内容（顺序上在被引入的文件之后）。
                state["block"] = None
                state["start"] = i + 2
                continue
            segment.append(line)
        flush()

    def flush(self, file: str, text: str, block: tuple | None, start_line: int) -> None:
        if block is None and not text.strip():
            return
        content = self.expand_shared_inputs(uncomment(text))
        parts = split_headings(content)
        emitted_block = False
        heading_only: Heading | None = None
        for kind, value, offset in parts:
            line = start_line + offset
            if kind == "heading":
                value.file, value.line = file, line
                self.items.append(value)
                if block is not None and not emitted_block and heading_only is None:
                    heading_only = value
                continue
            cleaned = STRUCTURAL.sub("", value)
            if not cleaned.strip():
                self.transition(cleaned, had_content=False)
                continue
            if block is not None and not emitted_block:
                bid, mline, raw, metadata = block
                env = [entry.snapshot() for entry in self.stack]
                self.items.append(Block(bid, file, mline, raw, cleaned, env, metadata, part=self.part))
                emitted_block = True
                heading_only = None
                self.transition(cleaned)
                continue
            # 无编号的文字：按顶层空行切成段（补充材料没有编号行时尤其需要）。
            owner = block[0] if block is not None else None
            for sub_offset, chunk in split_unmarked(cleaned):
                if not chunk.strip():
                    self.transition(chunk, had_content=False)
                    continue
                env = [entry.snapshot() for entry in self.stack]
                chunk_line = line + sub_offset
                if owner is not None:
                    count = self.pseudo_counter.get(owner, 1) + 1
                    self.pseudo_counter[owner] = count
                    pid = f"~{owner}~{count}"
                elif self.part == "supp":
                    stem = PurePosixPath(file).stem
                    count = self.pseudo_counter.get(stem, 0) + 1
                    self.pseudo_counter[stem] = count
                    pid = f"S:{stem}:{count}"
                else:
                    pid = f"~{file}:{chunk_line}"
                self.items.append(Block(pid, file, chunk_line, chunk, chunk, env, {}, unmarked=True, part=self.part))
                if self.part != "supp":
                    self.warnings.append({"code": "unmarked_text", "lang": self.lang, "file": file, "line": chunk_line})
                self.transition(chunk)
        if block is not None and not emitted_block:
            bid, mline, raw, metadata = block
            if heading_only is not None:
                heading_only.block_id = bid
                heading_only.raw = raw  # type: ignore[attr-defined]
                heading_only.metadata = metadata  # type: ignore[attr-defined]
            else:
                env = [entry.snapshot() for entry in self.stack]
                self.items.append(Block(bid, file, mline, raw, "", env, metadata, part=self.part))

    def transition(self, content: str, had_content: bool = True) -> None:
        """按块内的 \\begin/\\end/\\item 更新跨块环境栈。

        块开始时已打开的环境：本块属于它的一部分（parts+1）。本块新打开且未闭合的环境：
        只有 \\begin 之后确有可见内容时才算已有一部分，否则下一块是它的第一部分（显示表框上沿等）。
        """
        opened_here: dict[int, int] = {}
        for entry in self.stack:
            if had_content:
                entry.parts += 1
        i = 0
        n = len(content)
        pattern = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}|\\item(?![A-Za-z])")
        while True:
            match = pattern.search(content, i)
            if not match:
                break
            k, slashes = match.start() - 1, 0
            while k >= 0 and content[k] == "\\":
                slashes += 1
                k -= 1
            if slashes % 2:
                i = match.end()
                continue
            if match[1] is None:
                if self.stack and self.stack[-1].name in LIST_ENVS:
                    self.stack[-1].items += 1
                i = match.end()
                continue
            name = match[2].strip()
            if name == "document":
                i = match.end()
                continue
            if match[1] == "begin":
                if name in VERBATIM_ENVS:
                    found = find_env_end(content, match.end(), name)
                    i = found[1] if found else n
                    continue
                try:
                    _, end = read_env_args(content, match.end(), name)
                except TexError:
                    end = match.end()
                self.stack.append(EnvEntry(name, content[match.start() : end]))
                opened_here[id(self.stack[-1])] = end
                i = end
            else:
                for depth in range(len(self.stack) - 1, -1, -1):
                    if self.stack[depth].name == name:
                        if depth != len(self.stack) - 1:
                            self.warnings.append({"code": "env_mismatch", "lang": self.lang, "env": name})
                        del self.stack[depth:]
                        break
                else:
                    self.warnings.append({"code": "unbalanced_end", "lang": self.lang, "env": name})
                i = match.end()
        for entry in self.stack:
            start = opened_here.get(id(entry))
            if start is not None:
                entry.parts = 1 if has_visible(content[start:]) else 0


DECLARATIONS = re.compile(
    r"\\(?:centering|raggedright|raggedleft|noindent|par|smallskip|medskip|bigskip|small|footnotesize|scriptsize"
    r"|tiny|normalsize|large|Large|relax|null|leavevmode|hfill|vfill)(?![A-Za-z])"
    r"|\\(?:setlength|addtolength|renewcommand|setcounter|addtocounter)\s*\{[^{}]*\}\s*\{[^{}]*\}"
    r"|\\(?:vspace|hspace)\*?\s*\{[^{}]*\}"
    r"|\\(?:medmuskip|thinmuskip|thickmuskip|arraystretch|tabcolsep|arrayrulewidth|extrarowheight)\s*=?\s*[-+]?[\d.]*\s*(?:pt|mu|em|ex|mm|cm|in|bp)?"
    r"|\\(?:begin|end)\s*\{[^{}]+\}(?:\[[^\]]*\])?(?:\{[^{}]*\})*"
)


def has_visible(text: str) -> bool:
    """去掉版式声明与环境边界后是否还有内容。"""
    return bool(DECLARATIONS.sub("", text).strip())


def split_unmarked(text: str) -> list[tuple[int, str]]:
    """在花括号与环境深度为 0 的空行处切分，返回 [(行偏移, 文本)]。"""
    chunks: list[tuple[int, str]] = []
    depth = 0
    env_depth = 0
    start = 0
    i = 0
    n = len(text)
    pattern = re.compile(r"\\(begin|end)\s*\{[^{}]+\}")
    while i < n:
        c = text[i]
        if c == "\\":
            m = pattern.match(text, i)
            if m:
                env_depth += 1 if m[1] == "begin" else -1
                env_depth = max(env_depth, 0)
                i = m.end()
                continue
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == "\n" and depth == 0 and env_depth == 0:
            m = re.match(r"\n[ \t]*\n\s*", text[i:])
            if m:
                chunk = text[start:i]
                chunks.append((text.count("\n", 0, start), chunk))
                i += m.end()
                start = i
                continue
        i += 1
    chunks.append((text.count("\n", 0, start), text[start:]))
    return chunks


def split_headings(content: str) -> list[tuple[str, object, int]]:
    """把内容按顶层 \\section 等标题切开，返回 [('content', 文本, 行偏移) | ('heading', Heading, 行偏移)]。"""
    parts: list[tuple[str, object, int]] = []
    depth_at = brace_depths(content)
    pos = 0
    for match in HEADING.finditer(content):
        if match.start() < pos or depth_at[match.start()] != 0:
            continue
        k, slashes = match.start() - 1, 0
        while k >= 0 and content[k] == "\\":
            slashes += 1
            k -= 1
        if slashes % 2:
            continue
        try:
            short, i = read_optional(content, match.end())
            title, i = read_group(content, i, allow_par=False)
        except TexError:
            continue
        if title is None:
            continue
        label = None
        j = skip_ws(content, i, allow_par=False)
        lm = re.match(r"\\label\s*\{([^{}]+)\}", content[j:])
        if lm:
            label = lm[1].strip()
            i = j + lm.end()
        before = content[pos : match.start()]
        if before.strip():
            parts.append(("content", before, content.count("\n", 0, pos)))
        elif before:
            parts.append(("content", before, content.count("\n", 0, pos)))
        heading = Heading(match[1], bool(match[2]), title, label, "", 0)
        parts.append(("heading", heading, content.count("\n", 0, match.start())))
        pos = i
    rest = content[pos:]
    if rest or not parts:
        parts.append(("content", rest, content.count("\n", 0, pos)))
    return parts


def brace_depths(text: str) -> list[int]:
    depths = [0] * (len(text) + 1)
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        depths[i] = depth
        c = text[i]
        if c == "\\" and i + 1 < n:
            depths[i + 1] = depth
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        i += 1
    depths[n] = depth
    return depths


def walk_language(
    provider: FileProvider, lang: str, include_supplement: bool = False, main: str = "main.tex"
) -> LangWalk:
    lang_root = "zh" if lang == "zh" else ""
    walker = Walker(provider, lang, lang_root)
    result = walker.walk(main)
    if include_supplement:
        supp = walker.resolve("supplementary.tex")
        if supp and (not lang_root or supp.startswith(lang_root + "/")):
            supp_walker = Walker(provider, lang, lang_root, part="supp")
            try:
                supp_result = supp_walker.walk("supplementary.tex")
            except FileNotFoundError:
                supp_result = None
            if supp_result:
                result.items.extend(supp_result.items)
                result.files.extend(supp_result.files)
                result.warnings.extend(supp_result.warnings)
                result.preamble += "\n" + supp_result.preamble
    return result


def preamble_sources(provider: FileProvider, lang: str, preamble: str, depth: int = 0) -> list[tuple[str, str]]:
    """导言区及其 \\input 的文件（宏、语言词、共享表体），返回 [(相对路径, 文本)]。"""
    walker = Walker(provider, lang, "zh" if lang == "zh" else "")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def visit(text: str, level: int) -> None:
        for match in INPUT.finditer(uncomment(text)):
            rel = walker.resolve(match[1])
            if rel is None or rel in seen or level > 6:
                continue
            seen.add(rel)
            try:
                body = provider.read(rel)
            except (OSError, FileNotFoundError, subprocess.CalledProcessError):
                continue
            found.append((rel, body))
            visit(body, level + 1)

    found.append(("<preamble>", preamble))
    visit(preamble, depth)
    return found


def match_env_body(content: str, start: int, name: str) -> tuple[str, int, bool]:
    """返回 (环境正文, 结束位置, 是否闭合)。"""
    found = find_env_end(content, start, name)
    if found is None:
        return content[start:], len(content), False
    return content[start : found[0]], found[1], True


__all__ = [
    "Block",
    "EnvEntry",
    "FileProvider",
    "GitProvider",
    "Heading",
    "LangWalk",
    "WorktreeProvider",
    "match_brace",
    "match_env_body",
    "preamble_sources",
    "split_headings",
    "walk_language",
]
