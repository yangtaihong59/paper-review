"""把编号块的 LaTeX 渲染成中间表示（IR），再序列化为 HTML 或用于逐字/逐词对比的记号。

原则：已知命令按语义渲染；版式声明（\\centering、字号等）有意忽略；未知宏以可见源码显示并计数，
绝不静默丢弃。数学式原文交给浏览器端的 KaTeX 渲染。
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field

from .macros import Macro, parse_definitions
from .texutil import (
    FLOAT_ENVS,
    LIST_ENVS,
    MATH_ENVS,
    TABULAR_ENVS,
    VERBATIM_ENVS,
    TexError,
    find_env_end,
    find_math_end,
    is_cjk,
    match_brace,
    read_arg,
    read_command,
    read_env_args,
    read_group,
    read_optional,
    skip_ws,
    split_top_level,
)

# ---------------------------------------------------------------------------
# IR
# ---------------------------------------------------------------------------


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict | None = None, children: list | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = children if children is not None else []

    def __repr__(self) -> str:  # pragma: no cover - 调试
        return f"<{self.tag} {self.attrs} {self.children!r}>"


class Math:
    __slots__ = ("tex", "display")

    def __init__(self, tex: str, display: bool = False):
        self.tex = tex
        self.display = display


VOID = {"img", "br", "hr", "wbr"}
BLOCK_TAGS = {"p", "div", "figure", "table", "ol", "ul", "dl", "pre", "details", "h1", "blockquote"}


def to_html(nodes: list) -> str:
    out: list[str] = []
    _html(nodes, out)
    return "".join(out)


def _attrs(attrs: dict) -> str:
    parts = []
    for key, value in attrs.items():
        if value is None or value is False:
            continue
        if value is True:
            parts.append(f" {key}")
        else:
            parts.append(f' {key}="{html.escape(str(value), quote=True)}"')
    return "".join(parts)


def _html(nodes: list, out: list[str]) -> None:
    for node in nodes:
        if isinstance(node, str):
            out.append(html.escape(node, quote=False))
        elif isinstance(node, Math):
            tex = html.escape(node.tex, quote=True)
            if node.display:
                out.append(f'<div class="math display" data-tex="{tex}">{html.escape(node.tex, quote=False)}</div>')
            else:
                out.append(f'<span class="math" data-tex="{tex}">{html.escape(node.tex, quote=False)}</span>')
        elif isinstance(node, Node):
            out.append(f"<{node.tag}{_attrs(node.attrs)}>")
            if node.tag not in VOID:
                _html(node.children, out)
                out.append(f"</{node.tag}>")


def visible_text(nodes: list) -> str:
    """IR 的可见文字（数学式以 $..$ 原文表示），用于可见性判断、标题纯文本与统计。"""
    out: list[str] = []

    def walk(items: list) -> None:
        for node in items:
            if isinstance(node, str):
                out.append(node)
            elif isinstance(node, Math):
                out.append(f"${node.tex}$")
            elif isinstance(node, Node):
                if node.tag == "img":
                    out.append("[img]")
                walk(node.children)

    walk(nodes)
    return "".join(out)


def has_visible_content(nodes: list) -> bool:
    return bool(visible_text(nodes).strip())


# ---------------------------------------------------------------------------
# 编号格式与解析
# ---------------------------------------------------------------------------


def roman(n: int) -> str:
    if n <= 0:
        return str(n)
    values = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
              (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for value, letters in values:
        while n >= value:
            out.append(letters)
            n -= value
    return "".join(out)


def alpha(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out or "0"


@dataclass
class LabelRef:
    number: str
    kind: str
    source: str  # aux | source
    page: str = ""
    source_number: str = ""


class RefResolver:
    """\\ref 的编号：构建产物（.aux）优先，缺失时用源码顺序推算的编号并标注。"""

    def __init__(self, aux_labels: dict, source_labels: dict[str, tuple[str, str]] | None = None):
        self.aux = aux_labels
        self.source = source_labels or {}

    def resolve(self, key: str) -> LabelRef | None:
        info = self.aux.get(key)
        src = self.source.get(key)
        if info is not None:
            kind = info.kind if info.anchor else (src[1] if src else "label")
            return LabelRef(info.number, kind, "aux", info.page, src[0] if src else "")
        if src is not None:
            return LabelRef(src[0], src[1], "source", "", src[0])
        return None


@dataclass
class BibInfo:
    number: str | None
    title: str
    text: str
    short: str
    year: str


class BibResolver:
    def __init__(self, cites: dict, entries: dict, bib_titles: dict, style: str):
        self.cites = cites
        self.entries = entries
        self.bib_titles = bib_titles
        self.style = style  # natbib | ieee
        self.used: set[str] = set()

    def info(self, key: str) -> BibInfo:
        cite = self.cites.get(key)
        entry = self.entries.get(key)
        title = entry.title if entry else self.bib_titles.get(key, "")
        return BibInfo(
            cite.number if cite else None,
            title,
            entry.text if entry else "",
            cite.short if cite else "",
            cite.year if cite else "",
        )

    def table(self) -> dict:
        out = {}
        for key in sorted(self.used):
            info = self.info(key)
            out[key] = {"n": info.number, "title": info.title, "text": info.text}
        return out


# ---------------------------------------------------------------------------
# 渲染器（每种语言一个；按文档顺序渲染以保持计数器）
# ---------------------------------------------------------------------------

TEXT_STYLE = {
    "textbf": ("strong", {}),
    "emph": ("em", {}),
    "textit": ("em", {}),
    "textsl": ("em", {}),
    "texttt": ("code", {}),
    "textsc": ("span", {"class": "sc"}),
    "textsf": ("span", {"class": "sf"}),
    "underline": ("u", {}),
    "uline": ("u", {}),
    "textsuperscript": ("sup", {}),
    "textsubscript": ("sub", {}),
    "textrm": None,
    "textup": None,
    "textmd": None,
    "textnormal": None,
    "mbox": None,
    "hbox": None,
    "text": None,
    "textcircled": ("span", {"class": "circled"}),
    "fbox": ("span", {"class": "fbox"}),
    "MakeUppercase": ("span", {"class": "upper"}),
    "uppercase": ("span", {"class": "upper"}),
    "IEEEmembership": ("span", {"class": "member"}),
    "IEEEauthorblockN": None,
    "IEEEauthorblockA": ("span", {"class": "affil"}),
    "ensuremath": "math",
}
DECLARATIONS = {
    "bfseries": ("strong", {}),
    "bf": ("strong", {}),
    "itshape": ("em", {}),
    "it": ("em", {}),
    "em": ("em", {}),
    "slshape": ("em", {}),
    "sl": ("em", {}),
    "ttfamily": ("code", {}),
    "tt": ("code", {}),
    "scshape": ("span", {"class": "sc"}),
    "sc": ("span", {"class": "sc"}),
    "sffamily": ("span", {"class": "sf"}),
    "rmfamily": None,
    "normalfont": None,
    "upshape": None,
    "mdseries": None,
}
IGNORE0 = {
    "centering", "raggedright", "raggedleft", "noindent", "indent", "tiny", "scriptsize", "footnotesize", "small",
    "normalsize", "large", "Large", "LARGE", "huge", "Huge", "relax", "null", "leavevmode", "protect", "nobreak",
    "allowbreak", "hfil", "vfill", "vfil", "smallskip", "medskip", "bigskip", "maketitle", "arraybackslash",
    "selectfont", "unskip", "ignorespaces", "strut", "break", "pagebreak", "nopagebreak", "newpage", "clearpage",
    "frenchspacing", "nonfrenchspacing", "normalcolor", "sloppy", "fussy", "balance", "IEEEoverridecommandlockouts",
    "IEEEpeerreviewmaketitle", "appendix", "linebreak", "noalign", "toprule", "midrule", "bottomrule", "hline",
    "morecmidrules", "footnotemark", "displaystyle", "textstyle", "boldmath", "unboldmath", "onecolumn", "twocolumn",
    "makeatletter", "makeatother", "cleardoublepage", "FloatBarrier", "suppressfloats", "item@",
}
# 命令名 → (可选参数个数, 必选参数个数)：读取并丢弃参数。
IGNORE_ARGS = {
    "setlength": (0, 2), "addtolength": (0, 2), "addtocounter": (0, 2), "vspace": (0, 1), "linespread": (0, 1),
    "fontsize": (0, 2), "pagestyle": (0, 1), "thispagestyle": (0, 1), "graphicspath": (0, 1), "captionsetup": (1, 1),
    "phantom": (0, 1), "hphantom": (0, 1), "vphantom": (0, 1), "rule": (1, 2), "bstctlcite": (0, 1), "nocite": (0, 1),
    "hypersetup": (0, 1), "addcontentsline": (0, 3), "cmidrule": (0, 1), "cline": (0, 1), "addlinespace": (1, 0),
    "specialrule": (0, 3), "rowcolor": (1, 1), "cellcolor": (1, 1), "arrayrulecolor": (1, 1), "newif": (0, 1),
    "IEEEtriggeratref": (0, 1), "IEEEtriggercmd": (0, 1), "bibliographystyle": (0, 1), "bibliography": (0, 1),
    "externaldocument": (1, 1), "pdfbookmark": (1, 2), "enlargethispage": (0, 1), "usetikzlibrary": (0, 1),
}
DIM_REGISTERS = {
    "medmuskip", "thinmuskip", "thickmuskip", "tabcolsep", "arraystretch", "arrayrulewidth", "extrarowheight",
    "parindent", "parskip", "baselineskip", "fboxsep", "fboxrule", "itemsep", "topsep", "parsep", "columnsep",
    "textfloatsep", "floatsep", "intextsep", "abovecaptionskip", "belowcaptionskip", "dbltextfloatsep",
}
SPACES = {
    ",": "\u2009", ";": " ", ":": " ", ">": " ", "!": "", " ": " ", "\t": " ", "\n": " ", "quad": "\u2003",
    "qquad": "\u2003\u2003", "enspace": "\u2002", "thinspace": "\u2009", "enskip": "\u2002", "space": " ",
    "nobreakspace": "\u00a0", "@": "", "/": "", "-": "", "hfill": " ", "hspace": None, "negthinspace": "",
}
SYMBOLS = {
    "%": "%", "&": "&", "#": "#", "$": "$", "_": "_", "{": "{", "}": "}", "textbackslash": "\\", "ldots": "…",
    "dots": "…", "textellipsis": "…", "textendash": "–", "textemdash": "—", "textasciitilde": "~",
    "textasciicircum": "^", "S": "§", "P": "¶", "dag": "†", "ddag": "‡", "textbullet": "•", "textdegree": "°",
    "copyright": "©", "textregistered": "®", "texttrademark": "™", "pounds": "£", "euro": "€", "textless": "<",
    "textgreater": ">", "textbar": "|", "textquotedblleft": "“", "textquotedblright": "”", "textquoteleft": "‘",
    "textquoteright": "’", "LaTeX": "LaTeX", "TeX": "TeX", "textmu": "µ", "textpm": "±", "texttimes": "×",
    "textperiodcentered": "·", "checkmark": "✓", "textdagger": "†", "slash": "/", "textunderscore": "_",
    "ss": "ß", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "o": "ø", "O": "Ø", "l": "ł",
    "L": "Ł", "i": "ı", "textsection": "§", "textquotesingle": "'", "lq": "‘", "rq": "’", "textexclamdown": "¡",
    "textquestiondown": "¿", "guillemotleft": "«", "guillemotright": "»", "textcelsius": "℃", "permil": "‰",
}
TEXT_MATH = {
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "lambda", "mu", "nu", "xi",
    "pi", "rho", "sigma", "tau", "phi", "varphi", "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi",
    "Pi", "Sigma", "Phi", "Psi", "Omega", "times", "pm", "mp", "le", "leq", "ge", "geq", "to", "approx", "sim",
    "infty", "cdot", "ast", "star", "circ", "bullet", "rightarrow", "leftarrow", "Rightarrow", "ne", "neq",
}
ACCENT_MARKS = {
    "'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304", ".": "\u0307",
    "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328", "r": "\u030a", "d": "\u0323",
    "b": "\u0331",
}
STAR_COMMANDS = {
    "section", "subsection", "subsubsection", "paragraph", "vspace", "hspace", "\\", "ref", "footnote", "caption",
    "cite", "citet", "citep", "citeauthor", "includegraphics", "newcommand", "renewcommand", "providecommand",
    "DeclareRobustCommand", "label", "item",
}
CITE_COMMANDS = {
    "cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar", "citenum", "Citet",
    "Citep", "Citeauthor", "citetext",
}
REF_COMMANDS = {"ref", "eqref", "pageref", "autoref", "cref", "Cref", "nameref", "vref", "Autoref", "subref"}
COLORS = {
    "red": "#c0392b", "blue": "#2563eb", "gray": "#808080", "grey": "#808080", "black": "currentColor",
    "green": "#2e8b57", "orange": "#d97706", "purple": "#7c3aed", "brown": "#8b5a2b", "darkgray": "#555555",
    "lightgray": "#b0b0b0", "cyan": "#0891b2", "magenta": "#c026d3", "yellow": "#ca8a04", "teal": "#0f766e",
    "violet": "#7c3aed", "olive": "#6b7d1f", "white": "#ffffff", "lime": "#65a30d", "pink": "#db2777",
}
INTERNAL_CTX = "@prctx"
DIM_VALUE = re.compile(
    r"\s*=?\s*[-+]?\d*\.?\d+\s*(?:pt|mu|em|ex|mm|cm|in|bp|sp|\\[A-Za-z]+)?"
    r"(?:\s*plus\s*[-+]?\d*\.?\d+\s*(?:pt|mu|em|fil+)?)?(?:\s*minus\s*[-+]?\d*\.?\d+\s*(?:pt|mu|em)?)?"
)
TEXT_RUN = re.compile(r"[^\\{}$%\s~`'\-]+")
WS_RUN = re.compile(r"[ \t\r\n]+")
CTRL_WS = re.compile(r"[ \t]*\n?[ \t]*")
BLOCK_LEVEL = {"p", "div", "figure", "table", "ol", "ul", "dl", "pre", "details", "h1", "blockquote", "thead", "tbody"}


def css_color(spec: str) -> str | None:
    """xcolor 名称（含 red!75!black 这类混色）→ CSS 颜色；无法识别时返回 None。"""
    spec = spec.strip()
    if spec in COLORS:
        return COLORS[spec]
    m = re.fullmatch(r"([A-Za-z]+)!(\d+)(?:!([A-Za-z]+))?", spec)
    if m and m[1] in COLORS:
        other = m[3] or "white"
        if other not in COLORS or COLORS[other] == "currentColor" and other != "black":
            return COLORS[m[1]]
        a, b = COLORS[m[1]], COLORS[other] if other != "black" else "#000000"
        if a == "currentColor":
            a = "#000000"
        pct = int(m[2]) / 100
        rgb = [round(int(a[i : i + 2], 16) * pct + int(b[i : i + 2], 16) * (1 - pct)) for i in (1, 3, 5)]
        return "#" + "".join(f"{v:02x}" for v in rgb)
    return None


@dataclass
class FloatCtx:
    kind: str
    label: str | None
    wide: bool


@dataclass
class BlockRender:
    nodes: list
    media: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    flags: set = field(default_factory=set)


class Renderer:
    def __init__(
        self,
        lang: str,
        macros: dict[str, Macro],
        tables: dict[tuple[str, str], str],
        refs: RefResolver,
        bib: BibResolver,
        figures,
        names: dict[str, str],
        doc_class: str = "IEEEtran",
    ):
        self.lang = lang
        self.macros = dict(macros)
        self.tables = tables
        self.refs = refs
        self.bib = bib
        self.figures = figures
        self.names = names
        self.doc_class = doc_class
        self.unknown: Counter = Counter()
        self.unknown_envs: Counter = Counter()
        self.unresolved_refs: Counter = Counter()
        self.unresolved_cites: Counter = Counter()
        self.ref_mismatch: dict[str, tuple[str, str]] = {}
        self.table_fallbacks: list[str] = []
        self.warnings: list[dict] = []
        self.source_labels: dict[str, tuple[str, str]] = {}
        self.counts: Counter = Counter()
        self.reset()

    # -- 计数器 ------------------------------------------------------------
    def reset(self) -> None:
        self.counters: Counter = Counter()
        self.heading_numbers: dict[str, str] = {}
        self.current_target: tuple[str, str] = ("section", "")
        self.last_char = ""
        self.float_stack: list[FloatCtx] = []
        self.block: BlockRender | None = None
        self.pending_ctx: tuple[int, int] | None = None

    def format_counter(self, kind: str, n: int) -> str:
        macro = self.macros.get("the" + kind)
        if macro and macro.safe:
            body = macro.body
            # \thetable = S\arabic{table} 之类。
            body = re.sub(r"\\arabic\s*\{[^{}]*\}", str(n), body)
            body = re.sub(r"\\Roman\s*\{[^{}]*\}", roman(n), body)
            body = re.sub(r"\\roman\s*\{[^{}]*\}", roman(n).lower(), body)
            body = re.sub(r"\\Alph\s*\{[^{}]*\}", alpha(n), body)
            body = re.sub(r"\\alph\s*\{[^{}]*\}", alpha(n).lower(), body)
            body = body.replace("\\the" + "section", self.heading_numbers.get("section", ""))
            if "\\" not in body:
                return body
        if self.doc_class == "IEEEtran":
            if kind == "table":
                return roman(n)
            return str(n)
        return str(n)

    def heading_number(self, level: str) -> str:
        if level == "part":
            self.counters["part"] += 1
            return roman(self.counters["part"])
        if level == "section":
            self.counters["section"] += 1
            self.counters["subsection"] = 0
            self.counters["subsubsection"] = 0
            num = self.format_counter("section", self.counters["section"])
            if num == str(self.counters["section"]) and self.doc_class == "IEEEtran":
                num = roman(self.counters["section"])
        elif level == "subsection":
            self.counters["subsection"] += 1
            self.counters["subsubsection"] = 0
            sec = self.heading_numbers.get("section", "")
            if self.doc_class == "IEEEtran":
                num = f"{sec}-{alpha(self.counters['subsection'])}"
            else:
                num = f"{sec}.{self.counters['subsection']}"
        else:
            self.counters["subsubsection"] += 1
            sub = self.heading_numbers.get("subsection", "")
            if self.doc_class == "IEEEtran":
                num = f"{sub}{self.counters['subsubsection']}"
            else:
                num = f"{sub}.{self.counters['subsubsection']}"
        self.heading_numbers[level] = num
        return num

    # -- 入口 ----------------------------------------------------------------
    def render_heading(self, level: str, starred: bool, title: str, label: str | None, aux_number: str | None = None) -> dict:
        number = "" if starred or level not in ("part", "section", "subsection", "subsubsection") else None
        if number is None:
            number = self.heading_number(level)
            self.current_target = (level, number)
            if label:
                self.source_labels[label] = (number, level)
        source_number = number
        if label:
            resolved = self.refs.resolve(label)
            if resolved and resolved.source == "aux" and not starred:
                number = resolved.number
        elif aux_number and not starred:
            number = aux_number
        self.last_char = ""
        nodes = self.inline(title)
        return {
            "number": number,
            "source_number": source_number,
            "html": to_html(nodes),
            "text": visible_text(nodes).strip(),
            "anchor": label,
            "nodes": nodes,
        }

    def render_block(self, content: str, env: list) -> BlockRender:
        self.block = BlockRender([])
        self.last_char = ""
        prefix = []
        for entry in env:
            prefix.append(f"\\{INTERNAL_CTX}{{{entry.parts}}}{{{entry.items}}}{entry.begin}")
        src = "".join(prefix) + content
        parser = Parser(self, src)
        nodes = parser.parse("flow")
        result = self.block
        result.nodes = nodes
        self.block = None
        return result

    def inline(self, text: str) -> list:
        return Parser(self, text).parse("inline")

    def flow(self, text: str) -> list:
        return Parser(self, text).parse("flow")

    # -- 名称 ----------------------------------------------------------------
    def caption_label(self, kind: str, number: str) -> str:
        if kind == "table":
            name = self.names.get("table", "TABLE")
            return f"{name} {number}"
        name = self.names.get("figure", "Fig.")
        return f"{name} {number}" + ("." if self.lang == "en" and self.doc_class == "IEEEtran" else "")

    def flag(self, name: str) -> None:
        if self.block is not None:
            self.block.flags.add(name)


class Builder:
    """收集解析结果：flow 模式按段落组织块级节点，inline 模式为平铺的行内节点。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.blocks: list = []
        self.inline: list = []
        self.wraps: list[tuple[str, dict, int]] = []

    def text(self, s: str) -> None:
        if not s:
            return
        if self.inline and isinstance(self.inline[-1], str):
            self.inline[-1] += s
        else:
            self.inline.append(s)

    def add(self, node) -> None:
        if isinstance(node, list):
            for item in node:
                self.add(item)
            return
        if isinstance(node, str):
            self.text(node)
        else:
            self.inline.append(node)

    def wrap(self, tag: str, attrs: dict) -> None:
        self.wraps.append((tag, attrs, len(self.inline)))

    def _close_wraps(self) -> None:
        while self.wraps:
            tag, attrs, start = self.wraps.pop()
            start = min(start, len(self.inline))
            children = self.inline[start:]
            if children:
                self.inline[start:] = [Node(tag, dict(attrs), children)]

    def par(self) -> None:
        if self.mode != "flow":
            self.text(" ")
            return
        self._flush_paragraph()

    def _flush_paragraph(self) -> None:
        wraps = list(self.wraps)
        self._close_wraps()
        content = self.inline
        self.inline = []
        # 段落中的样式声明延续到下一段（如 {\bfseries ...\par ...}）。
        self.wraps = [(tag, attrs, 0) for tag, attrs, _ in wraps]
        while content and isinstance(content[0], str) and not content[0].strip():
            content.pop(0)
        while content and isinstance(content[-1], str) and not content[-1].strip():
            content.pop()
        if not content:
            return
        if all(isinstance(n, Node) and n.tag == "a" and "anchor" in n.attrs.get("class", "")
               or isinstance(n, str) and not n.strip() for n in content):
            # 只有标签锚点的“段落”不包 <p>，免得多出空行。
            self.blocks.extend(n for n in content if isinstance(n, Node))
            return
        if isinstance(content[0], str):
            content[0] = content[0].lstrip()
        if isinstance(content[-1], str):
            content[-1] = content[-1].rstrip()
        self.blocks.append(Node("p", {}, content))

    def block(self, node) -> None:
        if self.mode != "flow":
            self.add(node)
            return
        self._flush_paragraph()
        self.blocks.append(node)

    def finish(self) -> list:
        if self.mode == "flow":
            self._flush_paragraph()
            return self.blocks
        self._close_wraps()
        return self.inline


class Parser:
    def __init__(self, r: Renderer, src: str):
        self.r = r
        self.src = src
        self.pos = 0
        self.expansions = 0

    # -- 主循环 --------------------------------------------------------------
    def parse(self, mode: str) -> list:
        out = Builder(mode)
        src = self.src
        while self.pos < len(self.src):
            src = self.src
            c = src[self.pos]
            if c == "\\":
                self.command(out)
            elif c == "{":
                self.group(out)
            elif c == "}":
                self.pos += 1
            elif c == "$":
                self.dollar(out)
            elif c == "%":
                end = src.find("\n", self.pos)
                self.pos = len(src) if end < 0 else end + 1
            elif c in " \t\r\n":
                self.whitespace(out, mode)
            elif c == "~":
                self.emit(out, "\u00a0")
                self.pos += 1
            elif c == "`":
                if src.startswith("``", self.pos):
                    self.emit(out, "“")
                    self.pos += 2
                else:
                    self.emit(out, "‘")
                    self.pos += 1
            elif c == "'":
                if src.startswith("''", self.pos):
                    self.emit(out, "”")
                    self.pos += 2
                else:
                    self.emit(out, "’")
                    self.pos += 1
            elif c == "-":
                if src.startswith("---", self.pos):
                    self.emit(out, "—")
                    self.pos += 3
                elif src.startswith("--", self.pos):
                    self.emit(out, "–")
                    self.pos += 2
                else:
                    self.emit(out, "-")
                    self.pos += 1
            else:
                # 普通文字成段读取。
                m = TEXT_RUN.match(src, self.pos)
                if m:
                    self.emit(out, m[0])
                    self.pos = m.end()
                else:
                    self.emit(out, c)
                    self.pos += 1
        return out.finish()

    def emit(self, out: Builder, text: str) -> None:
        out.text(text)
        if text:
            self.r.last_char = text[-1]

    def whitespace(self, out: Builder, mode: str) -> None:
        m = WS_RUN.match(self.src, self.pos)
        run = m[0]
        self.pos = m.end()
        if run.count("\n") >= 2:
            if mode == "flow":
                out.par()
                self.r.last_char = ""
            else:
                self.emit(out, " ")
            return
        if "\n" in run:
            # 中文行尾换行不产生空格（与 xeCJK 一致）。
            nxt = self.peek_visible_char()
            if is_cjk(self.r.last_char) and (is_cjk(nxt) or not nxt):
                return
            if not self.r.last_char:
                return
        if self.r.last_char in (" ", "\u00a0", "\n"):
            return
        self.emit(out, " ")

    def peek_visible_char(self) -> str:
        """下一个可见字符（越过 \\textbf{ 之类的外壳），用于判断换行处是否插空格。"""
        src = self.src
        i = self.pos
        for _ in range(4):
            if i >= len(src):
                return ""
            c = src[i]
            if c == "\\":
                m = re.compile(r"\\([A-Za-z]+)\*?\s*\{").match(src, i)
                if m and m[1] in ("textbf", "emph", "textit", "devnum", "revadd", "texttt", "mbox", "text"):
                    i = m.end()
                    continue
                return "\\"
            if c == "{":
                i += 1
                continue
            return c
        return ""

    def group(self, out: Builder) -> None:
        try:
            end = match_brace(self.src, self.pos)
        except TexError:
            self.pos += 1
            return
        inner = self.src[self.pos + 1 : end - 1]
        self.pos = end
        add_mixed(out, Parser(self.r, inner).parse("inline"))

    # -- 数学 ----------------------------------------------------------------
    def dollar(self, out: Builder) -> None:
        src = self.src
        if src.startswith("$$", self.pos):
            end = find_math_end(src, self.pos + 2, "$$")
            if end is None:
                self.emit(out, "$$")
                self.pos += 2
                return
            tex = src[self.pos + 2 : end]
            self.pos = end + 2
            self.display_math(out, tex, numbered=False)
            return
        end = find_math_end(src, self.pos + 1, "$")
        if end is None:
            self.emit(out, "$")
            self.pos += 1
            return
        tex = src[self.pos + 1 : end]
        self.pos = end + 1
        out.add(Math(clean_math(tex)))
        self.r.last_char = "$"

    def display_math(self, out: Builder, tex: str, numbered: bool, env: str = "equation") -> None:
        labels = re.findall(r"\\label\s*\{([^{}]+)\}", tex)
        body = tex
        if env in ("align", "align*", "flalign", "flalign*", "alignat", "alignat*", "eqnarray", "eqnarray*",
                   "gather", "gather*", "multline", "multline*"):
            rows = split_top_level(body, r"\\")
            new_rows = []
            star_env = env.rstrip("*")
            for row in rows:
                row_labels = re.findall(r"\\label\s*\{([^{}]+)\}", row)
                has_tag = re.search(r"\\tag\*?\s*\{", row)
                no_number = re.search(r"\\(?:nonumber|notag)\b", row)
                row = re.sub(r"\\label\s*\{[^{}]+\}|\\(?:nonumber|notag)\b", "", row)
                if numbered and not no_number and not has_tag and (row.strip() or row_labels) and star_env != "multline":
                    number = self.equation_number(row_labels)
                    row = row.rstrip() + f" \\tag{{{number}}}"
                new_rows.append(row)
            if numbered and star_env == "multline":
                number = self.equation_number(labels)
                new_rows[-1] = new_rows[-1].rstrip() + f" \\tag{{{number}}}"
            inner_env = {"eqnarray": "align", "flalign": "align", "alignat": "align", "multline": "gather"}.get(
                star_env, star_env
            )
            if star_env == "alignat":
                inner_env = "align"
            joined = " \\\\ ".join(new_rows)
            if inner_env == "gather" and star_env == "multline":
                body = "\\begin{gathered}" + joined + "\\end{gathered}"
            else:
                body = f"\\begin{{{inner_env}*}}" + joined + f"\\end{{{inner_env}*}}"
        else:
            no_number = re.search(r"\\(?:nonumber|notag)\b", body)
            has_tag = re.search(r"\\tag\*?\s*\{", body)
            body = re.sub(r"\\label\s*\{[^{}]+\}|\\(?:nonumber|notag)\b", "", body)
            if numbered and not no_number and not has_tag:
                number = self.equation_number(labels)
                body = body.rstrip() + f" \\tag{{{number}}}"
        for label in labels:
            if self.r.block is not None:
                self.r.block.labels.append(label)
        anchors = [Node("a", {"id": anchor_id(self.r.lang, label), "class": "anchor"}) for label in labels]
        self.r.flag("math-display")
        node = Math(clean_math(body), display=True)
        if anchors:
            out.block(Node("div", {"class": "math-wrap"}, anchors + [node]))
        else:
            out.block(node)
        self.r.last_char = ""

    def equation_number(self, labels: list[str]) -> str:
        self.r.counters["equation"] += 1
        number = self.r.format_counter("equation", self.r.counters["equation"])
        result = number
        for label in labels:
            self.r.source_labels[label] = (number, "equation")
            resolved = self.r.refs.resolve(label)
            if resolved and resolved.source == "aux":
                result = resolved.number
        return result

    # -- 命令 ----------------------------------------------------------------
    def command(self, out: Builder) -> None:
        src = self.src
        start = self.pos
        name, end = read_command(src, self.pos)
        if name == INTERNAL_CTX:
            self.pos = end
            parts, items = self.take(), self.take()
            try:
                self.r.pending_ctx = (int(parts), int(items))
            except ValueError:
                self.r.pending_ctx = (0, 0)
            return
        star = False
        if name in STAR_COMMANDS and end < len(src) and src[end] == "*":
            star = True
            end += 1
        self.pos = end
        if name and (name[0].isalpha() or name[0] == "@"):
            # 控制词后的空白（含单个换行）被 TeX 吞掉；空行（段落）保留。
            m = CTRL_WS.match(src, self.pos)
            if m and src[m.end() : m.end() + 1] != "\n":
                self.pos = m.end()
        # 优先级：内建语义 → 稿件导言区的安全宏 → 符号/空白/样式表 → 未知（可见源码）。
        handler = getattr(self, "c_" + name, None) if name.isalpha() else None
        if handler is not None:
            handler(out, name, star, start)
            return
        macro = self.r.macros.get(name)
        if macro is not None and macro.safe and self.expansions < 400:
            self.expand_macro(out, macro, start)
            return
        if name in SYMBOLS:
            self.emit(out, SYMBOLS[name])
            return
        if name in SPACES and SPACES[name] is not None:
            if SPACES[name]:
                self.emit(out, SPACES[name])
            return
        if name in ACCENT_MARKS:
            self.accent(out, name)
            return
        if name == "\\":
            self.c_newline(out, name, star, start)
            return
        if name == "(":
            close = find_math_end(src, self.pos, r"\)")
            if close is not None:
                out.add(Math(clean_math(src[self.pos : close])))
                self.pos = close + 2
                return
        if name == "[":
            close = find_math_end(src, self.pos, r"\]")
            if close is not None:
                tex = src[self.pos : close]
                self.pos = close + 2
                self.display_math(out, tex, numbered=False)
                return
        if name in TEXT_STYLE:
            self.styled(out, name)
            return
        if name in DECLARATIONS:
            spec = DECLARATIONS[name]
            if spec:
                out.wrap(*spec)
            return
        if name in IGNORE0:
            return
        if name in IGNORE_ARGS:
            nopt, nreq = IGNORE_ARGS[name]
            self.skip_args(nopt, nreq)
            return
        if name in DIM_REGISTERS:
            m = DIM_VALUE.match(src, self.pos)
            if m:
                self.pos = m.end()
            return
        if name in TEXT_MATH:
            out.add(Math("\\" + name))
            return
        self.unknown(out, name, star, start)

    def skip_args(self, nopt: int, nreq: int) -> None:
        for _ in range(nopt):
            _, self.pos = read_optional(self.src, self.pos)
        # cmidrule(lr){..}、\addlinespace[..]
        if self.src.startswith("(", self.pos):
            close = self.src.find(")", self.pos)
            if close > 0:
                self.pos = close + 1
        for _ in range(nreq):
            value, end = read_group(self.src, self.pos)
            if value is None:
                try:
                    _, end = read_arg(self.src, self.pos)
                except TexError:
                    return
            self.pos = end

    def take(self) -> str:
        """读取一个必选参数并前移；没有参数时返回空串。"""
        try:
            value, end = read_arg(self.src, self.pos)
        except TexError:
            return ""
        self.pos = end
        return value

    def take_optional(self) -> str | None:
        value, end = read_optional(self.src, self.pos)
        self.pos = end
        return value

    def expand_macro(self, out: Builder, macro: Macro, start: int) -> None:
        args = []
        if macro.nargs:
            if macro.default is not None:
                opt, self.pos = read_optional(self.src, self.pos)
                args.append(macro.default if opt is None else opt)
            while len(args) < macro.nargs:
                args.append(self.take())
        expansion = macro.expand(args)
        self.expansions += 1
        self.src = self.src[: start] + expansion + self.src[self.pos :]
        self.pos = start

    def unknown(self, out: Builder, name: str, star: bool, start: int) -> None:
        """未知宏：以可见源码显示命令名，参数按行内内容渲染（不丢弃）。"""
        self.r.unknown["\\" + name] += 1
        head = "\\" + name + ("*" if star else "")
        children: list = [Node("code", {"class": "unk-name"}, [head])]
        for _ in range(3):
            opt, end = read_optional(self.src, self.pos)
            if opt is not None and not re.search(r"\s", self.src[self.pos : self.pos + 1]):
                children.append(Node("code", {"class": "unk-arg"}, [f"[{opt}]"]))
                self.pos = end
                continue
            if self.src.startswith("{", self.pos):
                value, end = read_group(self.src, self.pos)
                if value is None:
                    break
                children.append(Node("code", {"class": "unk-brace"}, ["{"]))
                children.extend(self.r.inline(value))
                children.append(Node("code", {"class": "unk-brace"}, ["}"]))
                self.pos = end
                continue
            break
        out.add(Node("span", {"class": "unk", "title": f"未识别的宏 {head}（按源码显示）"}, children))

    def styled(self, out: Builder, name: str) -> None:
        if name == "makebox" or name == "framebox":
            self.take_optional()
            self.take_optional()
        arg = self.take()
        spec = TEXT_STYLE[name]
        if spec == "math":
            out.add(Math(clean_math(arg)))
            return
        children = self.r.inline(arg)
        if spec is None:
            out.add(children)
        else:
            out.add(Node(spec[0], dict(spec[1]), children))

    def accent(self, out: Builder, name: str) -> None:
        arg = self.take()
        letter = arg.strip()
        if letter.startswith("\\"):
            letter = SYMBOLS.get(letter[1:], letter[1:])
        if not letter:
            return
        import unicodedata

        self.emit(out, unicodedata.normalize("NFC", letter[0] + ACCENT_MARKS[name]) + letter[1:])

    # -- 具体命令 ------------------------------------------------------------
    def c_newline(self, out, name, star, start):
        self.take_optional()
        out.add(Node("br"))
        self.r.last_char = "\n"

    def c_par(self, out, name, star, start):
        out.par()
        self.r.last_char = ""

    def c_hspace(self, out, name, star, start):
        self.take()
        self.emit(out, " ")

    def c_color(self, out, name, star, start):
        self.take_optional()
        color = css_color(self.take())
        if color:
            out.wrap("span", {"style": f"color:{color}"})

    def c_textcolor(self, out, name, star, start):
        self.take_optional()
        color = css_color(self.take())
        children = self.r.inline(self.take())
        out.add(Node("span", {"style": f"color:{color}"} if color else {"class": "colored"}, children))

    def c_colorbox(self, out, name, star, start):
        self.take_optional()
        self.take()
        out.add(Node("span", {"class": "colorbox"}, self.r.inline(self.take())))

    def c_fcolorbox(self, out, name, star, start):
        self.take()
        self.take()
        out.add(Node("span", {"class": "fbox"}, self.r.inline(self.take())))

    def c_makebox(self, out, name, star, start):
        self.take_optional()
        self.take_optional()
        out.add(self.r.inline(self.take()))

    c_framebox = c_makebox

    def c_parbox(self, out, name, star, start):
        self.take_optional()
        self.take_optional()
        self.take_optional()
        self.take()
        out.add(Node("span", {"class": "parbox"}, self.r.inline(self.take())))

    def c_raisebox(self, out, name, star, start):
        self.take()
        self.take_optional()
        self.take_optional()
        out.add(self.r.inline(self.take()))

    def c_resizebox(self, out, name, star, start):
        self.take()
        self.take()
        add_mixed(out, self.r.inline(self.take()))

    def c_scalebox(self, out, name, star, start):
        self.take()
        self.take_optional()
        out.add(self.r.inline(self.take()))

    def c_texorpdfstring(self, out, name, star, start):
        first = self.take()
        self.take()
        out.add(self.r.inline(first))

    def c_verb(self, out, name, star, start):
        if self.pos < len(self.src):
            delim = self.src[self.pos]
            end = self.src.find(delim, self.pos + 1)
            if end > 0:
                out.add(Node("code", {}, [self.src[self.pos + 1 : end]]))
                self.pos = end + 1

    def c_url(self, out, name, star, start):
        url = self.take()
        if re.match(r"https?://", url):
            out.add(Node("a", {"href": url, "target": "_blank", "rel": "noopener noreferrer", "class": "url"}, [url]))
        else:
            out.add(Node("code", {}, [url]))

    c_nolinkurl = c_url

    def c_href(self, out, name, star, start):
        url = self.take()
        children = self.r.inline(self.take())
        if re.match(r"https?://|mailto:", url):
            out.add(Node("a", {"href": url, "target": "_blank", "rel": "noopener noreferrer"}, children))
        else:
            out.add(children)

    def c_setcounter(self, out, name, star, start):
        counter = self.take().strip()
        value = self.take().strip()
        if value.lstrip("-").isdigit():
            self.r.counters[counter] = int(value)

    def c_stepcounter(self, out, name, star, start):
        self.r.counters[self.take().strip()] += 1

    c_refstepcounter = c_stepcounter

    def c_newcommand(self, out, name, star, start):
        # 块内定义：登记后供后文使用，不渲染。
        snippet_start = start
        try:
            if self.src.startswith("{", self.pos):
                end = match_brace(self.src, self.pos)
            else:
                _, end = read_command(self.src, self.pos)
            _, end = read_optional(self.src, end)
            _, end = read_optional(self.src, end)
            j = skip_ws(self.src, end)
            end = match_brace(self.src, j)
        except TexError:
            return
        for macro in parse_definitions(self.src[snippet_start:end], "<block>"):
            self.r.macros[macro.name] = macro
        self.pos = end

    c_renewcommand = c_newcommand
    c_providecommand = c_newcommand
    c_DeclareRobustCommand = c_newcommand

    def c_def(self, out, name, star, start):
        m = re.compile(r"\s*\\([A-Za-z@]+)\s*\{").match(self.src, self.pos)
        if not m:
            return
        try:
            end = match_brace(self.src, m.end() - 1)
        except TexError:
            return
        self.r.macros[m[1]] = Macro(m[1], 0, None, self.src[m.end() : end - 1], "<block>")
        self.pos = end

    def c_TD(self, out, name, star, start):
        table = self.take().strip()
        row = self.take().strip()
        body = self.r.tables.get((table, row))
        if body is None:
            self.r.warnings.append({"code": "undefined_table_data", "lang": self.r.lang, "key": f"{table}/{row}"})
            out.add(Node("span", {"class": "unk", "title": "未定义的共享表格行"}, [f"\\TD{{{table}}}{{{row}}}"]))
            return
        self.src = self.src[: self.pos] + body + self.src[self.pos :]

    def c_TDdef(self, out, name, star, start):
        self.take()
        self.take()
        self.take()

    def c_devnum(self, out, name, star, start):
        children = self.r.inline(self.take())
        out.add(Node("span", {"class": "devnum", "title": "开发池数字（\\devnum）"}, children))

    def c_todo(self, out, name, star, start):
        children = self.r.inline(self.take())
        out.add(Node("span", {"class": "todo"}, [Node("b", {}, ["TODO"]), " "] + children))

    def c_revadd(self, out, name, star, start):
        out.add(Node("span", {"class": "revadd", "title": "修订：新增（\\revadd）"}, self.r.inline(self.take())))

    def c_revdel(self, out, name, star, start):
        out.add(Node("span", {"class": "revdel", "title": "修订：删去的原文（\\revdel）"}, self.r.inline(self.take())))

    c_revdelblk = c_revdel

    def c_label(self, out, name, star, start):
        key = self.take().strip()
        if not key:
            return
        kind, number = self.r.current_target
        self.r.source_labels.setdefault(key, (str(number), kind))
        if self.r.block is not None:
            self.r.block.labels.append(key)
        out.add(Node("a", {"id": anchor_id(self.r.lang, key), "class": "anchor"}))

    def ref_node(self, key: str, eq: bool = False, page: bool = False, name: str = "ref") -> Node:
        resolved = self.r.refs.resolve(key)
        if resolved is None:
            self.r.unresolved_refs[key] += 1
            return Node("span", {"class": "xref unresolved", "title": f"未解析的标签 {key}：不在 .aux 中，也不在源码中"},
                        ["??"])
        text = resolved.page if page else resolved.number
        cls = "xref"
        title = f"{key} → {resolved.kind} {resolved.number}"
        if resolved.page:
            title += f"（第 {resolved.page} 页）"
        if resolved.source == "source":
            cls += " from-source"
            title += "；.aux 中没有此标签，编号由源码顺序推算（PDF 可能未重新编译）"
        elif resolved.source_number and resolved.source_number != resolved.number and not page:
            cls += " stale"
            self.r.ref_mismatch[key] = (resolved.number, resolved.source_number)
            title += f"；按当前源码应为 {resolved.source_number}（PDF 可能过期）"
        if eq and not page:
            text = f"({text})"
        prefix = ""
        if name in ("autoref", "cref", "Cref", "Autoref"):
            prefix = {"figure": "Fig. ", "table": "Table ", "equation": "Eq. ", "section": "Sec. ",
                      "subsection": "Sec. ", "subsubsection": "Sec. "}.get(resolved.kind, "")
            if self.r.lang == "zh":
                prefix = {"figure": "图 ", "table": "表 ", "equation": "式 ", "section": "第 ",
                          "subsection": "第 "}.get(resolved.kind, "")
        return Node("a", {"class": cls, "href": "#" + anchor_id(self.r.lang, key), "title": title}, [prefix + text])

    def c_ref(self, out, name, star, start):
        keys = [k.strip() for k in self.take().split(",") if k.strip()]
        for index, key in enumerate(keys):
            if index:
                self.emit(out, ", ")
            out.add(self.ref_node(key, eq=name == "eqref", page=name == "pageref", name=name))
        self.r.last_char = "0"
        self.r.counts["refs"] += len(keys)

    c_eqref = c_pageref = c_autoref = c_cref = c_Cref = c_nameref = c_vref = c_Autoref = c_subref = c_ref

    def c_hyperref(self, out, name, star, start):
        key = self.take_optional()
        children = self.r.inline(self.take())
        if key:
            out.add(Node("a", {"class": "xref", "href": "#" + anchor_id(self.r.lang, key)}, children))
        else:
            out.add(children)

    def c_cite(self, out, name, star, start):
        opt1 = self.take_optional()
        opt2 = self.take_optional()
        keys = [k.strip() for k in self.take().split(",") if k.strip()]
        self.r.counts["cites"] += len(keys)
        pre, post = (opt1, opt2) if opt2 is not None else (None, opt1)
        nodes = self.cite_nodes(name, keys, pre, post)
        out.add(nodes)
        self.r.last_char = "]"

    def cite_nodes(self, name: str, keys: list[str], pre: str | None, post: str | None) -> list:
        bib = self.r.bib
        infos = []
        for key in keys:
            bib.used.add(key)
            info = bib.info(key)
            if info.number is None:
                self.r.unresolved_cites[key] += 1
            infos.append((key, info))

        def num_node(key: str, info: BibInfo) -> Node:
            if info.number is None:
                return Node("span", {"class": "cite unresolved", "data-cite": key, "title": f"未解析的引文 {key}"}, ["?"])
            return Node("span", {"class": "cite", "data-cite": key}, [info.number])

        if name in ("citeauthor", "Citeauthor"):
            return [", ".join(info.short or key for key, info in infos)]
        if name in ("citeyear", "citeyearpar"):
            return [", ".join(info.year or "?" for key, info in infos)]
        if name in ("citet", "Citet", "citealt"):
            nodes: list = []
            for index, (key, info) in enumerate(infos):
                if index:
                    nodes.append(", ")
                nodes.append((info.short or key) + "\u00a0[")
                nodes.append(num_node(key, info))
                if post and index == len(infos) - 1:
                    nodes.append(", " + post)
                nodes.append("]")
            return nodes
        # 数字引用：排序并压缩连续编号。
        numbered = []
        unresolved = []
        for key, info in infos:
            if info.number is not None and info.number.isdigit():
                numbered.append((int(info.number), key, info))
            else:
                unresolved.append((key, info))
        numbered.sort()
        groups: list[list] = []
        for item in numbered:
            if groups and item[0] == groups[-1][-1][0] + 1:
                groups[-1].append(item)
            elif groups and item[0] == groups[-1][-1][0]:
                continue
            else:
                groups.append([item])
        pieces: list[list] = []
        for group in groups:
            if len(group) >= 3:
                pieces.append([num_node(group[0][1], group[0][2]), "–", num_node(group[-1][1], group[-1][2])])
            else:
                for item in group:
                    pieces.append([num_node(item[1], item[2])])
        for key, info in unresolved:
            pieces.append([num_node(key, info)])
        nodes = []
        if name == "citenum":
            for index, piece in enumerate(pieces):
                if index:
                    nodes.append(", ")
                nodes.extend(piece)
            return nodes
        if self.r.bib.style == "natbib" or name in ("citep", "Citep", "citealp"):
            brackets = name != "citealp"
            if brackets:
                nodes.append("[")
            if pre:
                nodes.append(pre + " ")
            for index, piece in enumerate(pieces):
                if index:
                    nodes.append(", ")
                nodes.extend(piece)
            if post:
                nodes.append(", " + post)
            if brackets:
                nodes.append("]")
            return [Node("span", {"class": "citegroup"}, nodes)]
        # IEEE（cite 宏包）：[1]–[3], [5]
        for index, piece in enumerate(pieces):
            if index:
                nodes.append(", ")
            if len(piece) == 3:
                nodes.extend(["[", piece[0], "]–[", piece[2], "]"])
            else:
                nodes.extend(["["] + piece + ["]"])
        if post and nodes:
            nodes[-1] = ", " + post + "]"
        return [Node("span", {"class": "citegroup"}, nodes)]

    def c_footnote(self, out, name, star, start):
        self.take_optional()
        children = self.r.inline(self.take())
        out.add(Node("span", {"class": "fn"}, [Node("span", {"class": "fn-mark"}, ["注"]), " "] + children))

    c_thanks = c_footnote
    c_footnotetext = c_footnote

    def c_IEEEPARstart(self, out, name, star, start):
        first = self.take()
        rest = self.take()
        out.add([Node("span", {"class": "parstart"}, self.r.inline(first)),
                 Node("span", {"class": "parstart-rest"}, self.r.inline(rest))])

    def c_title(self, out, name, star, start):
        self.take_optional()
        self.r.flag("title")
        out.block(Node("h1", {"class": "paper-title"}, self.r.inline(self.take())))

    def c_author(self, out, name, star, start):
        self.take_optional()
        self.r.flag("author")
        out.block(Node("div", {"class": "paper-author"}, self.r.inline(self.take())))

    def c_markboth(self, out, name, star, start):
        left = self.r.inline(self.take())
        right = self.r.inline(self.take())
        self.r.flag("running-head")
        label = "页眉" if self.r.lang == "zh" else "Running heads"
        out.block(Node("div", {"class": "running-head"}, [label + ": "] + left + [" / "] + right))

    def c_and(self, out, name, star, start):
        self.emit(out, ", ")

    def c_caption(self, out, name, star, start):
        self.take_optional()
        text = self.take()
        ctx = self.r.float_stack[-1] if self.r.float_stack else FloatCtx("figure", None, False)
        kind = ctx.kind
        self.r.counters[kind] += 1
        number = self.r.format_counter(kind, self.r.counters[kind])
        self.r.current_target = (kind, number)
        shown = number
        if ctx.label:
            self.r.source_labels[ctx.label] = (number, kind)
            resolved = self.r.refs.resolve(ctx.label)
            if resolved and resolved.source == "aux":
                shown = resolved.number
        children = self.r.inline(text)
        label = Node("span", {"class": "cap-label"}, [self.r.caption_label(kind, shown)])
        out.block(Node("div", {"class": f"caption {kind}-caption"}, [label, " "] + children))

    def c_includegraphics(self, out, name, star, start):
        self.take_optional()
        path = self.take().strip()
        path = self.expand_text_macros(path)
        ref = self.r.figures.register(self.r.lang, path) if self.r.figures is not None else None
        self.r.flag("image")
        if ref is None:
            out.add(Node("span", {"class": "missing-fig", "title": "找不到图片文件", "data-img": path},
                         ["图片未找到：", path]))
            return
        if self.r.block is not None:
            self.r.block.media.append(ref.as_dict())
        out.add(Node("img", {"class": "fig-img", "src": ref.url, "alt": path, "loading": "lazy", "data-fig": ref.id}))

    def expand_text_macros(self, text: str) -> str:
        for _ in range(5):
            m = re.search(r"\\([A-Za-z]+)", text)
            if not m:
                break
            macro = self.r.macros.get(m[1])
            if not macro or macro.nargs:
                break
            text = text[: m.start()] + macro.body + text[m.end() :]
        return text.replace("{", "").replace("}", "")

    def c_item(self, out, name, star, start):
        label = self.take_optional()
        out.add(Node("span", {"class": "item-bullet"}, [label if label is not None else "•", " "]))

    def c_section(self, out, name, star, start):
        self.take_optional()
        title = self.r.inline(self.take())
        out.block(Node("div", {"class": f"inline-heading {name}"}, title))

    c_subsection = c_subsubsection = c_section

    def c_paragraph(self, out, name, star, start):
        self.take_optional()
        out.add(Node("strong", {"class": "run-in"}, self.r.inline(self.take())))
        self.emit(out, " ")

    c_subparagraph = c_paragraph

    def c_multicolumn(self, out, name, star, start):
        self.take()
        self.take()
        out.add(self.r.inline(self.take()))

    def c_multirow(self, out, name, star, start):
        self.take_optional()
        self.take()
        self.take_optional()
        self.take()
        self.take_optional()
        out.add(self.r.inline(self.take()))

    def c_input(self, out, name, star, start):
        target = self.take()
        out.add(Node("span", {"class": "unk", "title": "未展开的 \\input"}, [f"\\input{{{target}}}"]))
        self.r.unknown["\\input"] += 1

    # -- 环境 ----------------------------------------------------------------
    def c_begin(self, out, name, star, start):
        env = self.take().strip()
        ctx = None
        if self.r.pending_ctx is not None:
            ctx, self.r.pending_ctx = self.r.pending_ctx, None
        virtual = ctx is not None
        parts, items = ctx if ctx else (0, 0)
        try:
            args, body_start = read_env_args(self.src, self.pos, env)
        except TexError:
            args, body_start = [], self.pos
        if env in VERBATIM_ENVS:
            found = find_env_end(self.src, body_start, env)
            body_end, after = (found if found else (len(self.src), len(self.src)))
            if env != "comment":
                out.block(Node("pre", {"class": "verbatim"}, [self.src[body_start:body_end]]))
            self.pos = after
            return
        found = find_env_end(self.src, body_start, env)
        if found:
            body, after, closed = self.src[body_start : found[0]], found[1], True
        else:
            body, after, closed = self.src[body_start:], len(self.src), False
        self.pos = after
        self.environment(out, env, args, body, closed, virtual, parts, items)

    def c_end(self, out, name, star, start):
        env = self.take().strip()
        self.r.warnings.append({"code": "render_unbalanced_end", "lang": self.r.lang, "env": env})

    def environment(self, out, env, args, body, closed, virtual, parts, items) -> None:
        r = self.r
        if env in MATH_ENVS:
            numbered = not env.endswith("*") and env not in ("displaymath", "math")
            if env == "math":
                out.add(Math(clean_math(body)))
                return
            self.display_math(out, body, numbered=numbered, env=env)
            return
        if env in FLOAT_ENVS:
            self.float_env(out, env, body, closed, virtual, parts)
            return
        if env in TABULAR_ENVS:
            self.tabular_env(out, env, args, body, closed, virtual, parts)
            return
        if env in LIST_ENVS or env == "tablenotes":
            self.list_env(out, env, args, body, closed, virtual, items)
            return
        if env == "abstract":
            r.flag("abstract")
            label = "摘要" if r.lang == "zh" else "Abstract"
            inner = r.flow(body)
            if not has_visible_content(inner) and not closed:
                return
            head = [] if (virtual and parts > 0) else [Node("div", {"class": "env-label"}, [label])]
            out.block(Node("div", {"class": "abstract"}, head + inner))
            return
        if env in ("IEEEkeywords", "keywords", "IEEEkeyword"):
            r.flag("keywords")
            label = "关键词" if r.lang == "zh" else "Index Terms"
            inner = r.inline(body)
            if not has_visible_content(inner) and not closed:
                return
            out.block(Node("div", {"class": "keywords"}, [Node("span", {"class": "env-label"}, [label]), " "] + inner))
            return
        if env in ("center", "flushleft", "flushright", "quote", "quotation", "verse", "minipage", "small",
                   "footnotesize", "scriptsize", "adjustbox", "threeparttable", "subfigure", "multicols", "landscape",
                   "sideways", "spacing", "singlespace", "IEEEbiography", "IEEEbiographynophoto", "document"):
            inner = r.flow(body)
            if not has_visible_content(inner):
                return
            out.block(Node("div", {"class": f"env env-{env}"}, inner))
            return
        if env in ("theorem", "lemma", "proposition", "corollary", "definition", "remark", "assumption", "example",
                   "proof", "problem"):
            label = {"proof": "Proof"}.get(env, env.capitalize())
            out.block(Node("div", {"class": "env env-theorem"}, [Node("span", {"class": "env-label"}, [label + ". "])]
                           + r.flow(body)))
            return
        # 未知环境：保留边界为可见源码，正文照常渲染。
        r.unknown_envs[env] += 1
        inner = r.flow(body)
        out.block(Node("div", {"class": "unk-env", "title": f"未识别的环境 {env}"},
                       [Node("code", {"class": "unk-name"}, [f"\\begin{{{env}}}"])] + inner
                       + ([Node("code", {"class": "unk-name"}, [f"\\end{{{env}}}"])] if closed else [])))

    def float_env(self, out, env, body, closed, virtual, parts) -> None:
        r = self.r
        kind = "table" if env.startswith("table") else "figure"
        wide = env.endswith("*")
        label = None
        cap = re.search(r"\\caption\b", body)
        for m in re.finditer(r"\\label\s*\{([^{}]+)\}", body):
            if cap is None or m.start() > cap.start():
                label = m[1].strip()
                break
        r.float_stack.append(FloatCtx(kind, label, wide))
        saved_target = r.current_target
        try:
            inner = r.flow(body)
        finally:
            r.float_stack.pop()
            r.current_target = saved_target
        if not has_visible_content(inner) and not closed:
            return
        r.flag("float-" + kind)
        if wide:
            r.flag("wide")
        classes = ["float", kind]
        if wide:
            classes.append("wide")
        if virtual and parts > 0:
            classes.append("cont-top")
            r.flag("float-part")
        if not closed:
            classes.append("cont-bottom")
            r.flag("float-part")
        out.block(Node("figure", {"class": " ".join(classes)}, inner))

    def list_env(self, out, env, args, body, closed, virtual, items) -> None:
        r = self.r
        pieces = split_items(body)
        lead, entries = pieces[0], pieces[1:]
        nodes: list = []
        lead_nodes = r.flow(lead) if lead.strip() else []
        if has_visible_content(lead_nodes):
            nodes.extend(lead_nodes)
        lis = []
        for label, content in entries:
            children = r.flow(content)
            attrs = {}
            if label is not None:
                attrs["class"] = "custom-label"
                children = [Node("span", {"class": "item-label"}, r.inline(label))] + children
            lis.append(Node("li", attrs, children))
        if not lis and not has_visible_content(nodes):
            return
        r.flag("list")
        tag = "ol" if env == "enumerate" else ("dl" if env == "description" else "ul")
        if tag == "dl":
            tag = "ul"
        attrs = {"class": f"list list-{env}"}
        if tag == "ol":
            attrs["start"] = str(items + 1 if virtual else 1)
        out.block(Node(tag, attrs, nodes + lis) if not nodes else Node("div", {}, nodes + [Node(tag, attrs, lis)]))

    def tabular_env(self, out, env, args, body, closed, virtual, parts) -> None:
        r = self.r
        spec = args[-1] if args else ""
        try:
            node = build_table(self, spec or "", body)
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都降级为源码
            r.table_fallbacks.append(str(exc)[:200])
            node = Node("details", {"class": "table-source"},
                        [Node("summary", {}, ["表格未能解析为 HTML，展开查看 LaTeX 源码"]),
                         Node("pre", {}, [f"\\begin{{{env}}}{{{spec}}}\n{body}\n\\end{{{env}}}"])])
        if node is None:
            return
        r.flag("tabular")
        out.block(node)


for _cite in CITE_COMMANDS:
    setattr(Parser, "c_" + _cite, Parser.c_cite)
del _cite


def add_mixed(out: Builder, nodes: list) -> None:
    """把子解析结果并入：块级节点（表格、列表、图）在 flow 模式下作为独立块，其余为行内。"""
    for node in nodes:
        if isinstance(node, Node) and node.tag in BLOCK_LEVEL and out.mode == "flow":
            out.block(node)
        elif isinstance(node, Math) and node.display and out.mode == "flow":
            out.block(node)
        else:
            out.add(node)


def anchor_id(lang: str, key: str) -> str:
    return f"L-{lang}-{key}"


def clean_math(tex: str) -> str:
    # KaTeX 不支持 mathtools 的 multlined：近似为 gathered。
    tex = re.sub(r"\\begin\s*\{multlined\}(?:\[[^\]]*\])?", r"\\begin{gathered}", tex)
    tex = re.sub(r"\\end\s*\{multlined\}", r"\\end{gathered}", tex)
    tex = re.sub(r"\\(?:mbox|hbox)\s*\{", r"\\text{", tex)
    tex = re.sub(r"\\intertext\s*\{([^{}]*)\}", r"\\text{\1}\\\\", tex)
    tex = re.sub(r"\\label\s*\{[^{}]*\}", "", tex)
    tex = re.sub(r"\\ensuremath\s*\{", "{", tex)
    return tex.strip()


def split_items(body: str) -> list:
    """按顶层 \\item 切分列表正文：[前导文本, (标签, 内容), ...]。"""
    pieces: list = []
    depth = 0
    env_depth = 0
    i = 0
    n = len(body)
    last = 0
    current_label: str | None = None
    first = True
    pattern = re.compile(r"\\(begin|end)\s*\{[^{}]+\}")
    while i < n:
        c = body[i]
        if c == "\\":
            m = pattern.match(body, i)
            if m:
                env_depth += 1 if m[1] == "begin" else -1
                i = m.end()
                continue
            if body.startswith("\\item", i) and not body[i + 5 : i + 6].isalpha() and depth == 0 and env_depth == 0:
                chunk = body[last:i]
                if first:
                    pieces.append(chunk)
                    first = False
                else:
                    pieces.append((current_label, chunk))
                j = i + 5
                label, j2 = read_optional(body, j)
                current_label = label
                last = j2 if label is not None else j
                i = last
                continue
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if first:
        pieces.append(body[last:])
    else:
        pieces.append((current_label, body[last:]))
    return pieces


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------

RULE = re.compile(
    r"\s*(\\(?:toprule|midrule|bottomrule|hline|morecmidrules|addlinespace|cline|cmidrule|specialrule|rowcolor"
    r"|noalign|hhline)\b)"
)


def parse_colspec(spec: str) -> list[str]:
    """列格式 → 每列对齐（l/c/r/p）。忽略 @{}、!{}、|、>{}/<{} 中的声明（但识别居中/右对齐 X 列）。"""
    cols: list[str] = []
    i = 0
    n = len(spec)
    pending = ""
    while i < n:
        c = spec[i]
        if c in " \t\n|":
            i += 1
        elif c in "@!":
            j = skip_ws(spec, i + 1)
            if j < n and spec[j] == "{":
                i = match_brace(spec, j)
            else:
                i = j + 1
        elif c in "><":
            j = skip_ws(spec, i + 1)
            if j < n and spec[j] == "{":
                end = match_brace(spec, j)
                decl = spec[j:end]
                if c == ">":
                    if "centering" in decl:
                        pending = "c"
                    elif "raggedleft" in decl:
                        pending = "r"
                    elif "raggedright" in decl:
                        pending = "l"
                i = end
            else:
                i = j + 1
        elif c == "*":
            count, j = read_arg(spec, i + 1)
            inner, j = read_arg(spec, j)
            try:
                repeat = int(count.strip())
            except ValueError:
                repeat = 1
            cols.extend(parse_colspec(inner) * repeat)
            i = j
        elif c in "lcr":
            cols.append(pending or c)
            pending = ""
            i += 1
        elif c in "pmbX":
            if c != "X":
                j = skip_ws(spec, i + 1)
                if j < n and spec[j] == "{":
                    i = match_brace(spec, j)
                else:
                    i = j
            else:
                i += 1
            cols.append(pending or "p")
            pending = ""
        elif c in "SsDdLCRJ":
            cols.append({"S": "r", "D": "r", "L": "l", "C": "c", "R": "r", "J": "p"}.get(c, "l"))
            i += 1
            j = skip_ws(spec, i)
            if j < n and spec[j] == "[":
                i = match_bracket_safe(spec, j)
            elif j < n and spec[j] == "{" and c in "DLCRJ":
                i = match_brace(spec, j)
        elif c == "{":
            i = match_brace(spec, i)
        else:
            i += 1
    return cols


def match_bracket_safe(spec: str, j: int) -> int:
    end = spec.find("]", j)
    return end + 1 if end >= 0 else len(spec)


def expand_table_data(parser: Parser, body: str) -> str:
    def replace(match: re.Match) -> str:
        key = (match[1].strip(), match[2].strip())
        data = parser.r.tables.get(key)
        if data is None:
            parser.r.warnings.append({"code": "undefined_table_data", "lang": parser.r.lang, "key": "/".join(key)})
            return match[0]
        return data

    return re.sub(r"\\TD\s*\{([^{}]+)\}\s*\{([^{}]+)\}", replace, body)


def build_table(parser: Parser, spec: str, body: str) -> Node | None:
    r = parser.r
    cols = parse_colspec(spec)
    body = expand_table_data(parser, body)
    # 表体中可能出现 \TD 之外的宏（如 \Lmm）；单元格渲染时再展开。
    segments = split_top_level(body, r"\\")
    rows: list[dict] = []
    pending_rules: list[tuple[str, str]] = []
    for index, seg in enumerate(segments):
        if index:
            # \\[2pt]、\\* 的参数属于上一行的换行命令。
            seg = re.sub(r"^\s*\*?\s*(?:\[\s*-?[\d.]+\s*(?:pt|mm|cm|em|ex|in|bp)\s*\])?", "", seg)
        rules, rest = take_rules(seg)
        pending_rules.extend(rules)
        if not rest.strip():
            continue
        rows.append({"rules": pending_rules, "text": rest})
        pending_rules = []
    trailing = pending_rules
    if not rows:
        if not has_rule_content(trailing):
            return None
        return None
    header_end = 0
    seen_top = False
    for index, row in enumerate(rows):
        kinds = [k for k, _ in row["rules"]]
        if "toprule" in kinds:
            seen_top = True
        if "midrule" in kinds or ("hline" in kinds and index > 0):
            header_end = index
            break
    if not seen_top and header_end == 0:
        header_end = 0
    covered: dict[int, int] = {}
    html_rows = []
    ncols = max(len(cols), 1)
    for index, row in enumerate(rows):
        cells_src = split_top_level(row["text"], "&")
        cells = []
        col = 0
        for cell_src in cells_src:
            while covered.get(col, 0) > 0:
                covered[col] -= 1
                col += 1
            cell = cell_src.strip()
            colspan, rowspan = 1, 1
            align = cols[col] if col < len(cols) else "l"
            m = re.match(r"\\multicolumn\s*\{\s*(\d+)\s*\}\s*", cell)
            if m:
                colspan = int(m[1])
                mspec, j = read_arg(cell, m.end())
                content, j = read_arg(cell, j)
                mcols = parse_colspec(mspec)
                align = mcols[0] if mcols else align
                cell = content + cell[j:]
            m = re.match(r"\\multirow\s*", cell)
            if m:
                j = m.end()
                _, j = read_optional(cell, j)
                nrows, j = read_arg(cell, j)
                _, j = read_optional(cell, j)
                _, j = read_arg(cell, j)
                _, j = read_optional(cell, j)
                content, j = read_arg(cell, j)
                try:
                    rowspan = max(1, int(nrows.strip()))
                except ValueError:
                    rowspan = 1
                cell = content + cell[j:]
            children = r.inline(cell)
            attrs = {"class": f"al-{align}"}
            if colspan > 1:
                attrs["colspan"] = str(colspan)
            if rowspan > 1:
                attrs["rowspan"] = str(rowspan)
                for k in range(col, col + colspan):
                    covered[k] = rowspan - 1
            cells.append((col, colspan, Node("th" if index < header_end else "td", attrs, children)))
            col += colspan
        # cmidrule：给对应列加上沿细线。
        classes = []
        for kind, arg in row["rules"]:
            if kind in ("toprule",):
                classes.append("rule-top")
            elif kind in ("midrule", "hline"):
                classes.append("rule-mid")
            elif kind in ("bottomrule",):
                classes.append("rule-bottom-prev")
            elif kind == "addlinespace":
                classes.append("gap")
            elif kind in ("cmidrule", "cline"):
                m = re.search(r"(\d+)\s*-\s*(\d+)", arg)
                if m:
                    lo, hi = int(m[1]) - 1, int(m[2]) - 1
                    for ccol, span, node in cells:
                        if ccol <= hi and ccol + span - 1 >= lo:
                            node.attrs["class"] += " cmid"
        html_rows.append(Node("tr", {"class": " ".join(classes)} if classes else {}, [c[2] for c in cells]))
    if any(kind == "bottomrule" for kind, _ in trailing) and html_rows:
        cls = html_rows[-1].attrs.get("class", "")
        html_rows[-1].attrs["class"] = (cls + " rule-bottom").strip()
    head = [row for i, row in enumerate(html_rows) if i < header_end]
    tail = [row for i, row in enumerate(html_rows) if i >= header_end]
    children = []
    if head:
        children.append(Node("thead", {}, head))
    children.append(Node("tbody", {}, tail))
    r.counts["tables"] += 1
    return Node("div", {"class": "table-wrap"}, [Node("table", {"class": "tex-table", "data-cols": str(ncols)},
                                                       children)])


def has_rule_content(rules: list) -> bool:
    return bool(rules)


def take_rules(seg: str) -> tuple[list[tuple[str, str]], str]:
    rules = []
    i = 0
    while True:
        m = RULE.match(seg, i)
        if not m:
            break
        kind = m[1][1:]
        j = m.end()
        arg = ""
        if kind in ("cmidrule", "cline"):
            if seg.startswith("(", skip_ws(seg, j)):
                j = seg.index(")", j) + 1
            _, j = read_optional(seg, j)
            value, j2 = read_group(seg, j)
            if value is not None:
                arg, j = value, j2
        elif kind == "addlinespace":
            _, j = read_optional(seg, j)
        elif kind == "specialrule":
            for _ in range(3):
                _, j = read_group(seg, j)
        elif kind in ("rowcolor", "noalign", "hhline"):
            _, j = read_optional(seg, j)
            _, j = read_group(seg, j)
        rules.append((kind, arg))
        i = j
    return rules, seg[i:]
