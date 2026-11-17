"""读取 LaTeX 构建产物：.aux 的标签编号与引文编号，.bbl 的文献条目。

编号以构建产物为准（与 PDF 一致）；构建缺失或过期时由渲染器用源码顺序推算并显式标注。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .texutil import TexError, match_brace, read_command, skip_ws


@dataclass
class LabelInfo:
    number: str
    page: str = ""
    title: str = ""
    anchor: str = ""

    @property
    def kind(self) -> str:
        """按 hyperref 锚点推断类型：figure / table / equation / section / subsection / ..."""
        head = self.anchor.split(".", 1)[0]
        head = re.sub(r"\*$", "", head)
        return head or "label"


@dataclass
class CiteInfo:
    number: str
    year: str = ""
    short: str = ""
    long: str = ""


@dataclass
class BibEntry:
    key: str
    text: str
    title: str


@dataclass
class AuxData:
    labels: dict[str, LabelInfo] = field(default_factory=dict)
    cites: dict[str, CiteInfo] = field(default_factory=dict)
    toc: list[tuple[str, str, str]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    mtime: float | None = None


def _groups(text: str, i: int, count: int) -> tuple[list[str], int]:
    out = []
    for _ in range(count):
        i = skip_ws(text, i)
        if i >= len(text) or text[i] != "{":
            break
        end = match_brace(text, i)
        out.append(text[i + 1 : end - 1])
        i = end
    return out, i


ACCENTS = {
    '"': "\u0308",
    "'": "\u0301",
    "`": "\u0300",
    "^": "\u0302",
    "~": "\u0303",
    "=": "\u0304",
    ".": "\u0307",
    "u": "\u0306",
    "v": "\u030c",
    "H": "\u030b",
    "c": "\u0327",
    "k": "\u0328",
    "r": "\u030a",
}
SPECIAL_LETTERS = {
    "i": "ı",
    "j": "ȷ",
    "o": "ø",
    "O": "Ø",
    "aa": "å",
    "AA": "Å",
    "ae": "æ",
    "AE": "Æ",
    "oe": "œ",
    "OE": "Œ",
    "ss": "ß",
    "l": "ł",
    "L": "Ł",
}


def accent_fold(text: str) -> str:
    """把 {\\"O}、\\'e、\\c{s} 等 TeX 重音转成 Unicode 字符。"""

    def repl(match: re.Match) -> str:
        letter = (match["braced"] or match["dotless"] or match["bare"]).strip()
        if letter.startswith("\\"):
            letter = SPECIAL_LETTERS.get(letter[1:], letter[1:])
        return unicodedata.normalize("NFC", letter + ACCENTS[match["acc"]])

    # 符号重音（\\"o、\\'{e}）可紧跟字母；字母重音（\\u、\\v、\\c…）后必须是空白或花括号，
    # 否则 \\url、\\vol 之类的控制词会被误认作重音。
    symbol = (
        r"\\(?P<acc>[\"'`^~=.])\s*"
        r"(?:\{(?P<braced>\\?[A-Za-z]{1,2})\}|(?P<dotless>\\[ij])(?![A-Za-z])|(?P<bare>[A-Za-z]))"
    )
    letter = (
        r"\\(?P<acc>[uvHckr])"
        r"(?:\s*\{(?P<braced>\\?[A-Za-z]{1,2})\}|\s+(?P<dotless>\\[ij])(?![A-Za-z])|\s+(?P<bare>[A-Za-z]))"
    )
    for pattern in (symbol, letter):
        # 整组外壳 {\\"O} 与内部形式一起替换。
        text = re.sub(r"\{" + pattern + r"\}", repl, text)
        text = re.sub(pattern, repl, text)
    text = re.sub(r"\\(aa|AA|ae|AE|oe|OE|ss|o|O|l|L)(?![A-Za-z])\s?", lambda m: SPECIAL_LETTERS[m[1]], text)
    return text


def tex_to_plain(text: str) -> str:
    """把文献条目、标签标题等短 TeX 片段转成可读纯文本（保留 $..$ 原文）。"""
    text = accent_fold(text)
    text = re.sub(r"\\(?:newblock|BIBentryALTinterwordspacing|BIBentrySTDinterwordspacing|relax|protect)\b\s*", "", text)
    text = re.sub(r"\\natexlab\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\bibinfo\{[^{}]*\}\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\url\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:emph|textit|textbf|textrm|texttt|textsc|mbox|BIBforeignlanguage\{[^{}]*\})\s*\{", "{", text)
    text = re.sub(r"\\(?:em|it|bf|rm|sc|tt)\b\s*", "", text)
    text = text.replace("``", "“").replace("''", "”").replace("---", "—").replace("--", "–")
    text = text.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_").replace("\\#", "#")
    text = re.sub(r"\\[,;:!> ]", " ", text)
    text = text.replace("~", " ")
    text = re.sub(r"\\[A-Za-z]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_number(text: str) -> str:
    """\\mbox  {III-A} → III-A；\\textup{..}、\\relax 等外壳去掉。"""
    text = re.sub(r"\\(?:mbox|textup|ensuremath|hbox|textnormal)\s*", "", text)
    text = re.sub(r"\\relax\b|\\protect\b", "", text)
    return tex_to_plain(text)


def parse_aux(path: Path, data: AuxData | None = None, prefix: str = "", depth: int = 0) -> AuxData:
    """读取 .aux（及其 \\@input 的子 aux）；prefix 用于 xr 的 \\externaldocument[前缀]。"""
    data = data or AuxData()
    if not path.is_file() or depth > 8:
        return data
    data.paths.append(str(path))
    stat = path.stat()
    data.mtime = max(data.mtime or 0, stat.st_mtime)
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"\\(newlabel|bibcite|@writefile|@input)\b", text):
        kind = match[1]
        i = match.end()
        try:
            if kind == "newlabel":
                groups, _ = _groups(text, i, 2)
                if len(groups) < 2:
                    continue
                key, value = groups
                inner, _ = _groups(value, 0, 4)
                if not inner:
                    continue
                info = LabelInfo(
                    number=clean_number(inner[0]),
                    page=inner[1] if len(inner) > 1 else "",
                    title=tex_to_plain(inner[2]) if len(inner) > 2 else "",
                    anchor=inner[3] if len(inner) > 3 else "",
                )
                data.labels.setdefault(prefix + key, info)
            elif kind == "bibcite" and not prefix:
                groups, _ = _groups(text, i, 2)
                if len(groups) < 2:
                    continue
                key, value = groups
                value = value.strip()
                if value.startswith("{"):
                    inner, _ = _groups(value, 0, 4)
                    number = inner[0] if inner else value
                    data.cites[key] = CiteInfo(
                        number=tex_to_plain(number),
                        year=tex_to_plain(inner[1]) if len(inner) > 1 else "",
                        short=tex_to_plain(inner[2]) if len(inner) > 2 else "",
                        long=tex_to_plain(inner[3]) if len(inner) > 3 else "",
                    )
                else:
                    data.cites[key] = CiteInfo(number=tex_to_plain(value))
            elif kind == "@writefile" and not prefix:
                groups, _ = _groups(text, i, 2)
                if len(groups) < 2 or groups[0] != "toc":
                    continue
                entry = groups[1]
                m = re.match(r"\s*\\contentsline\s*", entry)
                if not m:
                    continue
                parts, _ = _groups(entry, m.end(), 3)
                if len(parts) < 3:
                    continue
                level, title, page = parts
                number = ""
                num = re.match(r"\s*\\numberline\s*", title)
                if num:
                    ngroups, nend = _groups(title, num.end(), 1)
                    number = clean_number(ngroups[0]) if ngroups else ""
                    title = title[nend:]
                data.toc.append((level, number, tex_to_plain(title)))
            elif kind == "@input":
                groups, _ = _groups(text, i, 1)
                if groups:
                    parse_aux(path.parent / groups[0], data, prefix, depth + 1)
        except TexError:
            continue
    return data


def parse_bbl(path: Path) -> dict[str, BibEntry]:
    """读取 .bbl 的 \\bibitem 条目：键、清理后的全文与标题（``…'' 内的首段）。"""
    entries: dict[str, BibEntry] = {}
    if not path.is_file():
        return entries
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"(?m)%.*$", "", text)
    body_end = text.find("\\end{thebibliography}")
    if body_end >= 0:
        text = text[:body_end]
    starts = [m.start() for m in re.finditer(r"\\bibitem\b", text)]
    previous_authors = ""
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end]
        try:
            _, i = read_command(chunk, 0)
            i = skip_ws(chunk, i)
            if i < len(chunk) and chunk[i] == "[":
                depth = 0
                j = i
                while j < len(chunk):
                    if chunk[j] == "{":
                        depth += 1
                    elif chunk[j] == "}":
                        depth -= 1
                    elif chunk[j] == "]" and depth == 0:
                        break
                    j += 1
                i = j + 1
            i = skip_ws(chunk, i)
            if i >= len(chunk) or chunk[i] != "{":
                continue
            kend = match_brace(chunk, i)
            key = chunk[i + 1 : kend - 1].strip()
            raw = chunk[kend:].strip()
        except TexError:
            continue
        title_match = re.search(r"``(.+?)(?:,)?''", raw, re.S)
        title = tex_to_plain(title_match[1]).rstrip(",") if title_match else ""
        plain = tex_to_plain(raw)
        authors = plain.split("“", 1)[0].strip().rstrip(",")
        if plain.startswith("——") or plain.startswith("–—") or raw.lstrip().startswith("------"):
            plain = re.sub(r"^[—–-]+,?\s*", (previous_authors + ", ") if previous_authors else "", plain)
        elif authors:
            previous_authors = authors
        if not title:
            emph = re.search(r"\\emph\{([^{}]+)\}", raw)
            title = tex_to_plain(emph[1]) if emph else plain[:120]
        entries[key] = BibEntry(key, plain, title)
    return entries


def parse_bib_titles(paths: list[Path]) -> dict[str, str]:
    """后备：从 .bib 读取 title 字段（.bbl 缺失时用于提示框）。"""
    titles: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,", text):
            key = match[1]
            start = match.end()
            nxt = re.search(r"\n@\w+\s*\{", text[start:])
            chunk = text[start : start + nxt.start()] if nxt else text[start:]
            tm = re.search(r"\btitle\s*=\s*", chunk, re.I)
            if not tm:
                continue
            j = tm.end()
            try:
                if j < len(chunk) and chunk[j] == "{":
                    end = match_brace(chunk, j)
                    value = chunk[j + 1 : end - 1]
                elif j < len(chunk) and chunk[j] == '"':
                    end = chunk.index('"', j + 1)
                    value = chunk[j + 1 : end]
                else:
                    continue
            except (TexError, ValueError):
                continue
            titles.setdefault(key, tex_to_plain(value))
    return titles
