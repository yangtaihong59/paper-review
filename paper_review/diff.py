"""段落对比：把渲染 IR 切成记号（中文逐字、英文逐词，数学式整体为一个记号），再做序列差分。

每个记号带着所在的行内样式（粗体、\\devnum 高亮等）生成 HTML，因此对比视图仍可读；
样式也进入记号键，单纯加粗/去粗也会显示为改动。
"""

from __future__ import annotations

import difflib
import html
import re

from .render import Math, Node, to_html

ZH_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:[.'’-][A-Za-z0-9_]+)*|\s+|.", re.S)
EN_TOKEN = re.compile(r"[A-Za-z0-9_À-ɏ]+(?:[.'’-][A-Za-z0-9_À-ɏ]+)*|\s+|.", re.S)
KEEP_TAGS = {"strong", "em", "code", "sup", "sub", "u", "b"}
KEEP_CLASSES = {"devnum", "todo", "revadd", "revdel", "sc", "cite", "xref", "unk", "fn", "cap-label", "parstart"}
SKIP_CLASSES = {"anchor"}
# 胶囊样式的元素整体作为一个记号，否则逐词拆开后每个词都会画成一个胶囊。
ATOMIC_CLASSES = {"todo", "unk", "cite"}


Token = tuple[str, str]


def tokens(nodes: list, lang: str) -> list[Token]:
    out: list[Token] = []
    pattern = ZH_TOKEN if lang == "zh" else EN_TOKEN

    def emit_text(text: str, opens: list[str], closes: list[str], style: str) -> None:
        for match in pattern.finditer(text):
            piece = match[0]
            key = " " if piece.isspace() else piece
            body = " " if piece.isspace() else html.escape(piece, quote=False)
            out.append((style + key, "".join(opens) + body + "".join(reversed(closes))))

    def walk(items: list, opens: list[str], closes: list[str], style: str) -> None:
        for node in items:
            if isinstance(node, str):
                emit_text(node, opens, closes, style)
            elif isinstance(node, Math):
                out.append((style + ("D:" if node.display else "M:") + node.tex,
                            "".join(opens) + to_html([node]) + "".join(reversed(closes))))
            elif isinstance(node, Node):
                classes = set(node.attrs.get("class", "").split())
                if classes & SKIP_CLASSES:
                    continue
                if classes & ATOMIC_CLASSES:
                    from .render import visible_text

                    key = "A:" + ".".join(sorted(classes)) + ":" + visible_text([node])
                    out.append((style + key, "".join(opens) + to_html([node]) + "".join(reversed(closes))))
                    continue
                tag = node.tag
                if tag == "img" or "missing-fig" in classes:
                    # 以 LaTeX 中写的图文件名为键（URL 带版本号，不能参与比较）。
                    label = "[图]" if lang == "zh" else "[Figure]"
                    name = node.attrs.get("alt") or node.attrs.get("data-img", "")
                    out.append(("IMG:" + name, f'<span class="dimg">{label} {html.escape(name)}</span>'))
                    continue
                if tag == "br":
                    out.append(("\n", "<br>"))
                    continue
                if tag in ("p", "div", "figure", "ol", "ul", "pre", "h1", "details", "thead", "tbody", "table"):
                    walk(node.children, opens, closes, style)
                    if tag in ("p", "div", "h1", "pre"):
                        out.append(("¶", '<span class="dsep"></span>'))
                    continue
                if tag == "tr":
                    for index, cell in enumerate(node.children):
                        if index:
                            out.append(("|", ' <span class="dcell">|</span> '))
                        walk([cell], opens, closes, style)
                    out.append(("\n", "<br>"))
                    continue
                if tag in ("td", "th", "summary"):
                    walk(node.children, opens, closes, style)
                    continue
                if tag == "li":
                    out.append(("•", '<span class="dbullet">• </span>'))
                    walk(node.children, opens, closes, style)
                    out.append(("¶", '<span class="dsep"></span>'))
                    continue
                keep = tag in KEEP_TAGS or bool(classes & KEEP_CLASSES)
                if keep:
                    attrs = {k: v for k, v in node.attrs.items() if k in ("class", "data-cite", "title", "href", "style")}
                    attr_text = "".join(f' {k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
                    walk(node.children, opens + [f"<{tag}{attr_text}>"], closes + [f"</{tag}>"],
                         style + f"<{tag}.{'.'.join(sorted(classes))}>")
                else:
                    walk(node.children, opens, closes, style)

    walk(nodes, [], [], "")
    # 去掉首尾的段落分隔与空白记号。
    while out and out[-1][0] in ("¶", " ", "\n"):
        out.pop()
    while out and out[0][0] in ("¶", " ", "\n"):
        out.pop(0)
    return out


def keys(toks: list[Token]) -> list[str]:
    return [k for k, _ in toks]


def _small(op: tuple, old: list[Token], lang: str) -> bool:
    _, i1, i2, _, _ = op
    span = old[i1:i2]
    if lang == "zh":
        return len(span) <= 2 and all(len(k) <= 2 or k.isspace() for k, _ in span)
    return len(span) <= 1 and all(not re.search(r"[A-Za-z0-9]", k) for k, _ in span)


def diff_html(old: list[Token], new: list[Token], lang: str) -> str:
    a, b = keys(old), keys(new)
    if len(a) * len(b) > 40_000_000:
        # 极长段落：退化为整体替换，避免平方复杂度。
        ops = [("replace", 0, len(a), 0, len(b))]
    else:
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    # 合并被很短的相等片段隔开的改动，减少逐字对比的碎片。
    changed = True
    while changed:
        changed = False
        merged: list[tuple] = []
        i = 0
        while i < len(ops):
            op = ops[i]
            if (
                op[0] == "equal"
                and merged
                and merged[-1][0] != "equal"
                and i + 1 < len(ops)
                and ops[i + 1][0] != "equal"
                and _small(op, old, lang)
            ):
                prev, nxt = merged.pop(), ops[i + 1]
                merged.append(("replace", prev[1], nxt[2], prev[3], nxt[4]))
                i += 2
                changed = True
                continue
            merged.append(op)
            i += 1
        ops = merged
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.append("".join(h for _, h in new[j1:j2]))
            continue
        if i2 > i1:
            out.append('<del class="d">' + "".join(h for _, h in old[i1:i2]) + "</del>")
        if j2 > j1:
            out.append('<ins class="d">' + "".join(h for _, h in new[j1:j2]) + "</ins>")
    return "".join(out)


def whole(toks: list[Token], kind: str) -> str:
    """新增或删除的整段：全部记为插入或删除。"""
    tag = "ins" if kind == "added" else "del"
    return f'<{tag} class="d">' + "".join(h for _, h in toks) + f"</{tag}>"
