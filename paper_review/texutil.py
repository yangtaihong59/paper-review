"""LaTeX 源码的底层扫描工具：注释、花括号/方括号配对、命令与环境参数。

只做有界的字符串扫描，不执行 TeX。所有函数对不平衡输入抛出 TexError，调用方决定降级方式。
"""

from __future__ import annotations

import re

# 与 zh_en_sync.py 相同的编号行规则。
MARKER = re.compile(r"^\s*%%¶\s+(\S+)(.*?)\s*$")
INPUT = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")

# 环境的参数签名：o = 可选 [..]，m = 必选 {..}。未列出的环境只吃紧随的一个可选参数。
ENV_SIGNATURES: dict[str, str] = {
    "tabular": "om",
    "tabular*": "mom",
    "tabularx": "mm",
    "tabulary": "mm",
    "longtable": "om",
    "array": "om",
    "minipage": "ooom",
    "multicols": "m",
    "figure": "o",
    "figure*": "o",
    "table": "o",
    "table*": "o",
    "itemize": "o",
    "enumerate": "o",
    "description": "o",
    "subfigure": "om",
    "wrapfigure": "omm",
    "adjustbox": "m",
    "threeparttable": "o",
    "tablenotes": "o",
}

TABULAR_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "array"}
LIST_ENVS = {"itemize", "enumerate", "description"}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "wrapfigure", "SCfigure"}
MATH_ENVS = {
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "flalign",
    "flalign*",
    "alignat",
    "alignat*",
    "displaymath",
    "math",
}
VERBATIM_ENVS = {"verbatim", "verbatim*", "lstlisting", "minted", "comment"}


class TexError(ValueError):
    """源码结构无法可靠解析（花括号不平衡等）。"""


def uncomment(text: str) -> str:
    """移除非转义的 TeX 注释，保留换行与 \\%（与 zh_en_sync.uncomment 一致）。"""
    lines = []
    for line in text.splitlines():
        for match in re.finditer("%", line):
            prefix = line[: match.start()]
            if (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0:
                line = prefix
                break
        lines.append(line)
    return "\n".join(lines)


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", uncomment(text)).strip()


def match_brace(src: str, i: int) -> int:
    """src[i] == '{'；返回匹配 '}' 之后的位置。"""
    if i >= len(src) or src[i] != "{":
        raise TexError(f"位置 {i} 不是左花括号")
    depth = 0
    n = len(src)
    j = i
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise TexError("花括号不平衡")


def match_bracket(src: str, i: int) -> int:
    """src[i] == '['；返回匹配 ']' 之后的位置（跳过花括号组）。"""
    if i >= len(src) or src[i] != "[":
        raise TexError(f"位置 {i} 不是左方括号")
    n = len(src)
    j = i + 1
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            j = match_brace(src, j)
            continue
        if c == "]":
            return j + 1
        j += 1
    raise TexError("方括号不平衡")


def skip_ws(src: str, i: int, allow_par: bool = True) -> int:
    """跳过空白。allow_par=False 时不越过空行（段落边界）。"""
    n = len(src)
    j = i
    newlines = 0
    while j < n and src[j] in " \t\r\n":
        if src[j] == "\n":
            newlines += 1
            if newlines >= 2 and not allow_par:
                return i
        j += 1
    return j


def read_command(src: str, i: int) -> tuple[str, int]:
    """src[i] == '\\'；返回 (命令名, 结束位置)。控制词为字母（含 @），控制符为单个字符。"""
    if i >= len(src) or src[i] != "\\":
        raise TexError("不是命令")
    j = i + 1
    if j >= len(src):
        return "", j
    if src[j].isalpha() or src[j] == "@":
        k = j
        while k < len(src) and (src[k].isalpha() or src[k] == "@"):
            k += 1
        return src[j:k], k
    return src[j], j + 1


def read_group(src: str, i: int, allow_par: bool = False) -> tuple[str | None, int]:
    """读取紧随的 {..} 参数（可先跳过空白）。无参数时返回 (None, i)。"""
    j = skip_ws(src, i, allow_par=allow_par)
    if j < len(src) and src[j] == "{":
        end = match_brace(src, j)
        return src[j + 1 : end - 1], end
    return None, i


def read_optional(src: str, i: int) -> tuple[str | None, int]:
    """读取紧随的 [..] 可选参数（可先跳过同一段内的空白）。"""
    j = skip_ws(src, i, allow_par=False)
    if j < len(src) and src[j] == "[":
        end = match_bracket(src, j)
        return src[j + 1 : end - 1], end
    return None, i


def read_arg(src: str, i: int) -> tuple[str, int]:
    """读取一个必选参数：{..}、单个控制序列或单个字符（TeX 的未定界参数）。"""
    j = skip_ws(src, i)
    if j >= len(src):
        raise TexError("缺少参数")
    if src[j] == "{":
        end = match_brace(src, j)
        return src[j + 1 : end - 1], end
    if src[j] == "\\":
        _, end = read_command(src, j)
        return src[j:end], end
    return src[j], j + 1


def read_env_args(src: str, i: int, name: str) -> tuple[list[str | None], int]:
    """按签名读取 \\begin{name} 之后的参数，返回 (参数列表, 结束位置)。"""
    signature = ENV_SIGNATURES.get(name, "o")
    args: list[str | None] = []
    j = i
    for kind in signature:
        if kind == "o":
            value, j = read_optional(src, j)
            args.append(value)
        else:
            value, k = read_group(src, j, allow_par=False)
            if value is None:
                # 允许未定界的单 token 参数（如 \begin{tabular}l）。
                value, k = read_arg(src, j)
            args.append(value)
            j = k
    return args, j


BEGIN_END = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")


def find_env_end(src: str, i: int, name: str) -> tuple[int, int] | None:
    """从 i 开始找与 \\begin{name} 配对的 \\end{name}；返回 (正文结束, \\end 之后)，找不到返回 None。"""
    depth = 1
    for match in BEGIN_END.finditer(src, i):
        if match[2].strip() != name:
            continue
        # 转义的反斜杠（\\begin）不是命令。
        start = match.start()
        backslashes = 0
        k = start - 1
        while k >= 0 and src[k] == "\\":
            backslashes += 1
            k -= 1
        if backslashes % 2:
            continue
        depth += 1 if match[1] == "begin" else -1
        if depth == 0:
            return match.start(), match.end()
    return None


def find_math_end(src: str, i: int, closer: str) -> int | None:
    """从 i 找数学结束符（$、$$、\\)、\\]），跳过转义字符与花括号组；返回结束符起始位置。"""
    n = len(src)
    j = i
    while j < n:
        c = src[j]
        if c == "\\":
            if closer in (r"\)", r"\]") and src.startswith(closer, j):
                return j
            j += 2
            continue
        if c == "{":
            try:
                j = match_brace(src, j)
            except TexError:
                return None
            continue
        if c == "$" and closer.startswith("$"):
            if closer == "$$":
                if src.startswith("$$", j):
                    return j
                j += 1
                continue
            return j
        j += 1
    return None


def split_top_level(src: str, separator: str) -> list[str]:
    """在花括号深度 0、数学与嵌套环境之外按分隔符切分（分隔符为 '&' 或 '\\\\'）。"""
    parts = []
    depth = 0
    env_depth = 0
    in_math = False
    start = 0
    n = len(src)
    j = 0
    while j < n:
        c = src[j]
        if c == "\\":
            if separator == r"\\" and src.startswith(r"\\", j) and depth == 0 and env_depth == 0 and not in_math:
                parts.append(src[start:j])
                j += 2
                start = j
                continue
            name, end = read_command(src, j)
            if name in ("begin", "end"):
                # 单元格内的任何嵌套环境（tabular、minipage、数学环境）都整体保留。
                group, gend = read_group(src, end)
                if group is not None:
                    env_depth += 1 if name == "begin" else -1
                    j = gend
                    continue
            j = end
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "$":
            in_math = not in_math
        elif c == separator and separator == "&" and depth == 0 and env_depth == 0 and not in_math:
            parts.append(src[start:j])
            start = j + 1
        j += 1
    parts.append(src[start:])
    return parts


CJK_RANGES = (
    (0x2E80, 0x2FDF),
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3100, 0x312F),
    (0x3190, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
)


def is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in CJK_RANGES)


def fingerprint(text: str) -> str:
    """与 zh_en_sync.fingerprint 相同：去注释、折叠空白、去首尾空白后 SHA-1 前 8 位。"""
    import hashlib

    return hashlib.sha1(normalized(text).encode("utf-8")).hexdigest()[:8]
