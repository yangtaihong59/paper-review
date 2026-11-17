"""读取稿件导言区（shared/macros.tex、lang-*.tex 等）的宏定义。

只展开“安全”的宏：宏体不含条件、\\csname、\\def 等 TeX 编程原语。其余宏保持未知，
由渲染器以可见源码显示，绝不静默丢弃。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .texutil import TexError, match_brace, read_optional, skip_ws, uncomment

DEFINE = re.compile(r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*")
UNSAFE = re.compile(
    r"\\(?:if[A-Za-z]*|else|fi|csname|endcsname|expandafter|def|gdef|edef|let|makeatletter|makeatother"
    r"|PackageError|begingroup|endgroup|@[A-Za-z]+|noexpand|futurelet|afterassignment|relax\b|setbox|colorbox)"
)


@dataclass
class Macro:
    name: str
    nargs: int
    default: str | None
    body: str
    source: str

    @property
    def safe(self) -> bool:
        return not UNSAFE.search(self.body)

    def expand(self, args: list[str]) -> str:
        body = self.body
        # 从高位到低位替换，避免 #1 吞掉 #10。
        for index in range(len(args), 0, -1):
            body = body.replace(f"#{index}", args[index - 1])
        return body


def parse_definitions(text: str, source: str) -> list[Macro]:
    """解析 \\newcommand 系列与简单 \\def、\\DeclareMathOperator 定义。"""
    text = uncomment(text)
    found: list[Macro] = []
    for match in DEFINE.finditer(text):
        i = match.end()
        try:
            if text.startswith("{", i):
                end = match_brace(text, i)
                name_part = text[i + 1 : end - 1].strip()
                i = end
            else:
                m = re.match(r"\\([A-Za-z@]+|.)", text[i:])
                if not m:
                    continue
                name_part = "\\" + m[1]
                i += m.end()
            if not name_part.startswith("\\"):
                continue
            name = name_part[1:]
            nargs_text, i = read_optional(text, i)
            nargs = int(nargs_text) if nargs_text and nargs_text.strip().isdigit() else 0
            default, i = read_optional(text, i) if nargs else (None, i)
            j = skip_ws(text, i)
            if j >= len(text) or text[j] != "{":
                continue
            end = match_brace(text, j)
            found.append(Macro(name, nargs, default, text[j + 1 : end - 1], source))
        except (TexError, ValueError):
            continue
    for match in re.finditer(r"\\def\s*\\([A-Za-z@]+)\s*\{", text):
        try:
            end = match_brace(text, match.end() - 1)
        except TexError:
            continue
        found.append(Macro(match[1], 0, None, text[match.end() : end - 1], source))
    for match in re.finditer(r"\\DeclareMathOperator\*?\s*\{\\([A-Za-z]+)\}\s*\{", text):
        try:
            end = match_brace(text, match.end() - 1)
        except TexError:
            continue
        found.append(Macro(match[1], 0, None, r"\operatorname{" + text[match.end() : end - 1] + "}", source))
    return found


def katex_macros(macros: dict[str, Macro]) -> dict[str, str]:
    """导出给 KaTeX 的宏表（只含无可选参数、宏体不含 $ 的安全宏）。"""
    result = {}
    for name, macro in macros.items():
        if not macro.safe or macro.default is not None or "$" in macro.body:
            continue
        if re.search(r"\\(?:begin|end)\{(?:figure|table)", macro.body):
            continue
        result["\\" + name] = macro.body
    return result
