#!/usr/bin/env python3
"""IEEE 论文逐段中英同步门禁；仅依赖 Python 3.10+ 标准库。

退出码：0 通过，1 有同步/页数告警，2 参数、文件或 Git 操作错误。
指纹与边界见配套 paper-zh-en-sync skill；本工具不执行翻译或 LaTeX。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

MARKER = re.compile(r"^\s*%%¶\s+(\S+)(.*?)\s*$")
INPUT = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")
REF = re.compile(r"\\(ref|eqref|label|cite(?:p|t|alp|author|year)?)(?:\*)?\s*(?:\[[^\]]*\]\s*)*\{([^{}]+)\}")
STATE_NAME = "SYNC.json"
CONFIG_NAME = "sync-config.json"
DEFAULT_MAX_PAGES = 18
DEFAULT_ROOT = Path.cwd()


def uncomment(text: str) -> str:
    """移除非转义的 TeX 注释，保留换行与 \\%。"""
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
    """指纹归一化：去注释、折叠空白、去首尾空白；不改 TeX 内容。"""
    return re.sub(r"\s+", " ", uncomment(text)).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalized(text).encode("utf-8")).hexdigest()[:8]


@dataclass
class Paragraph:
    id: str
    file: str
    line: int
    body: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def sha(self) -> str:
        return fingerprint(self.body)


def warning(code: str, id: str = "", **details: object) -> dict:
    return {"code": code, "id": id, **details}


def parse_file(path: str, text: str) -> tuple[list[Paragraph], list[dict]]:
    """编号块从编号行后起，到下一个编号行（或文件末尾）之前。"""
    lines = text.splitlines(keepends=True)
    starts = [(i, m) for i, line in enumerate(lines) if (m := MARKER.match(line))]
    paragraphs = []
    warnings = []
    for pos, (i, marker) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        metadata = {}
        for token in marker[2].split():
            if "=" not in token:
                warnings.append(warning("invalid_marker", marker[1], file=path, line=i + 1))
                continue
            key, value = token.split("=", 1)
            metadata[key] = value
        body = "".join(lines[i + 1 : end])
        p = Paragraph(marker[1], path, i + 1, body, metadata)
        paragraphs.append(p)
        if not normalized(body):
            warnings.append(warning("empty_paragraph", p.id, file=path, line=i + 1))
    # 只在正文区域检查漏标；数学/列表环境属于编号块的一部分。
    active = path not in ("main.tex", "supplementary.tex")
    marked = False
    new_block = True
    consumed = False
    math_depth = 0
    for i, raw in enumerate(lines):
        line = uncomment(raw).strip()
        if "\\begin{document}" in line:
            active = True
        if not active:
            continue
        if MARKER.match(raw):
            marked, consumed, new_block = True, False, True
            continue
        math_depth += len(re.findall(r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|tabular\*?)\}", line))
        if line and math_depth == 0:
            prose = bool(re.match(r"[A-Za-z\u3400-\u9fff]|\\(?:emph|textbf|textit|caption|footnote)\b", line))
            if prose and new_block:
                if not marked or consumed:
                    warnings.append(warning("missing_marker", file=path, line=i + 1))
                consumed, new_block = True, False
        math_depth -= len(re.findall(r"\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?|tabular\*?)\}", line))
        if not line:
            new_block = True
    return paragraphs, warnings


@dataclass
class Document:
    paragraphs: list[Paragraph]
    warnings: list[dict]
    files: list[str]

    @property
    def by_id(self) -> dict[str, Paragraph]:
        return {p.id: p for p in self.paragraphs}


def load_document(files: dict[str, str]) -> Document:
    parsed = {}
    warnings = []
    seen = set()
    for path, text in sorted(files.items()):
        parsed[path], found = parse_file(path, text)
        warnings.extend(found)
        for p in parsed[path]:
            if p.id in seen:
                warnings.append(warning("duplicate_id", p.id, file=path, line=p.line))
            seen.add(p.id)
    ordered = []
    visited = set()

    def visit(path: str) -> None:
        if path in visited:
            return
        visited.add(path)
        paragraphs = {p.line: p for p in parsed[path]}
        for i, line in enumerate(files[path].splitlines(), 1):
            if i in paragraphs:
                ordered.append(paragraphs[i])
            for match in INPUT.finditer(uncomment(line)):
                target = match[1].removeprefix("./")
                target = target if target.endswith(".tex") else target + ".tex"
                if target in parsed:
                    visit(target)

    if "main.tex" in files:
        visit("main.tex")
    else:
        warnings.append(warning("missing_main", file="main.tex"))
    for path in sorted(files):
        visit(path)
    if not ordered:
        warnings.append(warning("no_paragraphs"))
    return Document(ordered, warnings, sorted(files))


def working_files(root: Path, language: str) -> dict[str, str]:
    base = root / "zh" if language == "zh" else root
    paths = [base / "main.tex", *sorted((base / "sections").glob("*.tex"))]
    # 默认只要求双语正文。只有建立了中文补充稿，才扩大到双语补充材料。
    if (root / "zh" / "supplementary.tex").is_file():
        paths += [base / "supplementary.tex", *sorted((base / "supp").glob("*.tex"))]
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in paths if p.is_file()}


def git(root: Path, *args: str, env: dict[str, str] | None = None, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, text=True, capture_output=True, env=env, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"Git 失败：{' '.join(args)}")
    return result.stdout.strip()


def repo_location(root: Path) -> tuple[Path, str]:
    repo = Path(git(root, "rev-parse", "--show-toplevel"))
    return repo, root.relative_to(repo).as_posix()


def historical_files(root: Path, revision: str) -> dict[str, str]:
    commit = git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
    repo, relative = repo_location(root)
    prefix = (relative + "/" if relative != "." else "") + "zh/"
    paths = git(repo, "ls-tree", "-r", "--name-only", commit, "--", prefix).splitlines()
    files = {}
    include_supplementary = (root / "zh" / "supplementary.tex").is_file() or prefix + "supplementary.tex" in paths
    for path in paths:
        local = path.removeprefix(prefix)
        is_main = local == "main.tex" or (local.startswith("sections/") and local.endswith(".tex"))
        is_supp = local == "supplementary.tex" or (local.startswith("supp/") and local.endswith(".tex"))
        if is_main or (include_supplementary and is_supp):
            # git() 去首尾空白只影响指纹已忽略的空白。
            files[local] = git(repo, "show", f"{commit}:{path}")
    return files


def table_data(root: Path) -> dict[tuple[str, str], str]:
    """读取稿件的 TDdef 三参数宏，保留数字单元格中的嵌套数学花括号。"""
    rows = {}
    for path in sorted((root / "shared" / "tables").glob("*.tex")):
        text = uncomment(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"\\TDdef\s*\{([^{}]+)\}\s*\{([^{}]+)\}\s*\{", text):
            start, end, depth = match.end(), match.end(), 1
            while end < len(text) and depth:
                if text[end] in "{}" and (end == 0 or text[end - 1] != "\\"):
                    depth += 1 if text[end] == "{" else -1
                end += 1
            if depth:
                raise ValueError(f"TDdef 花括号不完整：{path}")
            key = (match[1], match[2])
            if key in rows:
                raise ValueError(f"TDdef 重复定义：{key}")
            rows[key] = text[start : end - 1]
    return rows


def expand_shared(
    text: str, root: Path, stack: tuple[str, ...] = (), tables: dict[tuple[str, str], str] | None = None
) -> str:
    """只展开 shared/，不递归展开正文/宏定义；不执行 TeX。"""

    def replace(match: re.Match) -> str:
        name = match[1].removeprefix("../").removeprefix("./")
        if not name.startswith("shared/"):
            return ""
        name = name if name.endswith(".tex") else name + ".tex"
        path = (root / name).resolve()
        if not path.is_relative_to((root / "shared").resolve()):
            raise ValueError(f"shared 引用越界：{name}")
        if name in stack:
            raise ValueError(f"shared 循环引用：{name}")
        return expand_shared(path.read_text(encoding="utf-8"), root, (*stack, name), tables)

    expanded = INPUT.sub(replace, uncomment(text))
    calls = re.compile(r"\\TD\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    if calls.search(expanded):
        tables = table_data(root) if tables is None else tables

        def cells(match: re.Match) -> str:
            key = (match[1], match[2])
            if key not in tables:
                raise ValueError(f"未定义共享表格行：{key}")
            return tables[key]

        expanded = calls.sub(cells, expanded)
    expanded = re.sub(r"\\(?:mref|meq)\s*\{([^{}]+)\}", r"\\ref{M-\1}", expanded)
    expanded = re.sub(r"(\\placeholderfigure(?:wide)?\s*)\{([^{}]+)\}", r"\1{\\label{\2}}", expanded)
    return expanded


def references(text: str) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {key: set() for key in ("ref", "cite", "label", "eqref")}
    for match in REF.finditer(uncomment(text)):
        key = "cite" if match[1].startswith("cite") else match[1]
        found[key].update(s.strip() for s in match[2].split(",") if s.strip())
    return {key: sorted(value) for key, value in found.items()}


def numbers(text: str) -> list[str]:
    """提取数值集合；统一千分位、科学计数及常见 SI 单位。

    单位量转成 m/s/Hz 的基本尺度。百分数转成比例；引用键和路径不当数值。
    相同数值重复出现不增加集合计数。
    """
    text = unicodedata.normalize("NFKC", uncomment(text)).replace("−", "-")
    # 附录 D：2-D/3-D 是维度术语；数学式 $3-D$ 仍保留数值。
    text = re.sub(r"(?<![\w$])[23]-D(?![\w$])", " ", text, flags=re.I)
    text = REF.sub("", text)
    text = re.sub(r"\\(?:includegraphics|input|include)(?:\[[^\]]*\])?\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:,|;|:|!|quad\b|qquad\b|thinspace\b)", " ", text)
    text = re.sub(r"\\(?:mathrm|textrm|text|operatorname)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:mathrm|textrm)\s+([A-Za-z]+)", r"\1", text)
    text = re.sub(r"\\SI\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1 \2", text)
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"(?<=\d)[ ~\u00a0]+(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"--+|[–—](?=\d)", " to ", text)
    sci = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:\\times|\\cdot|[×·])\s*10\s*\^\s*\{?\s*([+-]?\d+)\s*\}?")
    text = sci.sub(lambda m: f"{m[1]}e{m[2]}", text)
    # TeX 控制字后可以直接跟数字，不能把 \times4 的 4 当版本后缀漏掉。
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    factors = {
        "km": "1000",
        "cm": "0.01",
        "mm": "0.001",
        "m": "1",
        "ms": "0.001",
        "us": "0.000001",
        "µs": "0.000001",
        "s": "1",
        "min": "60",
        "h": "3600",
        "Hz": "1",
        "kHz": "1000",
        "MHz": "1000000",
        "千米": "1000",
        "公里": "1000",
        "米": "1",
        "秒": "1",
        "分钟": "60",
        "%": "0.01",
    }
    token = re.compile(r"(?<![A-Za-z_\d.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?!\d)")
    units = "|".join(sorted(map(re.escape, factors), key=len, reverse=True))
    result = set()
    for match in token.finditer(text):
        tail = text[match.end() :]
        # 附录 D：$12.6$-h/$2$-km 的闭合数学定界符不阻断单位归一化。
        unit = re.match(r"[\s~$-]*(?:\\)?(" + units + r")(?![A-Za-z])", tail)
        if re.match(r"[A-Za-z_]", tail) and not unit and not re.match(r"(?:st|nd|rd|th)\b", tail):
            continue
        try:
            with localcontext() as ctx:
                ctx.prec = 50
                value = Decimal(match[1]) * Decimal(factors[unit[1]] if unit else "1")
                if unit:
                    denominator = re.match(r"\s*/\s*(" + units + r")(?![A-Za-z])", tail[unit.end() :])
                    if denominator:
                        value /= Decimal(factors[denominator[1]])
                result.add(str(value.normalize()) if value else "0")
        except InvalidOperation as exc:
            raise ValueError(f"无法解析数字：{match[1]}") from exc
    return sorted(result)


GREEK = {
    "lambda": "λ",
    "sigma": "σ",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "theta": "θ",
    "mu": "μ",
    "rho": "ρ",
    "phi": "φ",
    "pi": "π",
    "xi": "ξ",
    "tau": "τ",
    "eta": "η",
    "omega": "ω",
}


def shared_symbols(root: Path) -> dict[str, str]:
    """从共享零参数宏读取希腊符号别名，仅供术语可见文本检查。"""
    path = root / "shared" / "macros.tex"
    if not path.is_file():
        return {}
    symbols = {}
    text = uncomment(path.read_text(encoding="utf-8"))
    for match in re.finditer(r"\\newcommand\s*\{\\([A-Za-z]+)\}\s*\{", text):
        pos, end, depth = match.end(), match.end(), 1
        while end < len(text) and depth:
            if text[end] in "{}" and (end == 0 or text[end - 1] != "\\"):
                depth += 1 if text[end] == "{" else -1
            end += 1
        if depth:
            raise ValueError(f"共享符号宏花括号不完整：{match[1]}")
        greek = re.findall(r"\\(" + "|".join(GREEK) + r")(?![A-Za-z])", text[pos : end - 1])
        if greek:
            symbols[match[1]] = " ".join(GREEK[name] for name in greek)
    return symbols


def visible(text: str, symbols: dict[str, str] | None = None) -> str:
    text = REF.sub("", uncomment(text))
    for name, glyph in {**GREEK, **(symbols or {})}.items():
        text = re.sub(r"\\" + name + r"(?![A-Za-z])", glyph, text)
    text = re.sub(r"\\[A-Za-z]+\*?", "", text)
    text = text.replace("{", " ").replace("}", " ").replace("~", " ")
    text = text.replace("_", " ").replace("^", " ")
    text = text.replace("---", "—").replace("--", "–")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold()


def term_pattern(phrase: str) -> re.Pattern[str]:
    """附录 E：完整词边界匹配，允许复数及常见 -ed/-ing 词形。"""
    function_words = {
        "a",
        "an",
        "the",
        "in",
        "on",
        "of",
        "by",
        "to",
        "as",
        "and",
        "or",
        "with",
        "across",
        "from",
        "under",
        "per",
    }

    def inflections(word: str) -> str:
        variants = {word}
        if len(word) > 2 and word not in function_words:
            # -ch 有 /k/ 和 /tʃ/ 两种读音（epoch → epochs，batch → batches）。
            variants.add(word + "s")
            if word.endswith(("s", "x", "z", "ch", "sh")):
                variants.add(word + "es")
            if word.endswith("y") and word[-2] not in "aeiou":
                variants.update((word[:-1] + "ies", word[:-1] + "ied", word + "ing"))
            elif word.endswith("e"):
                variants.update((word + "d", word[:-1] + "ing"))
                if word.endswith("ie"):
                    variants.add(word[:-2] + "ying")
            else:
                variants.update((word + "ed", word + "ing"))
                if re.search(r"[^aeiou][aeiou][b-df-hj-np-tv-z]$", word):
                    variants.update((word + word[-1] + "ed", word + word[-1] + "ing"))
        return "(?:" + "|".join(sorted(variants, key=lambda v: (-len(v), v))) + ")"

    text = visible(phrase)
    parts = []
    position = 0
    for match in re.finditer(r"[a-z]+", text):
        parts.extend((re.escape(text[position : match.start()]), inflections(match[0])))
        position = match.end()
    parts.append(re.escape(text[position:]))
    pattern = "".join(parts).replace(r"\ ", r"\s+")
    return re.compile(r"(?<!\w)" + pattern + r"(?!\w)")


def glossary(root: Path) -> list[tuple[str, str, str]]:
    path = root / "glossary.tsv"
    if not path.is_file():
        raise ValueError(f"缺少术语表：{path}")
    entries = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for i, row in enumerate(csv.reader(handle, delimiter="\t"), 1):
            if not row or row[0].startswith("#") or not any(row):
                continue
            if row[0] in ("中文", "Chinese", "zh"):
                continue
            if len(row) != 3 or not row[0].strip() or not row[1].strip():
                raise ValueError(f"glossary.tsv:{i} 需要中文、英文、备注三列")
            english = row[1].strip()
            if any(not alternative.strip() for alternative in re.split(r"\s*\|\s*", english)):
                raise ValueError(f"glossary.tsv:{i} 可接受写法不能为空")
            entries.append((row[0].strip(), english, row[2].strip()))
    return entries


def read_state(root: Path) -> dict:
    path = root / STATE_NAME
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != "paper.zh_en_sync.v1":
        raise ValueError("SYNC.json schema 不支持")
    return state


def max_pages(root: Path) -> int:
    """稿件页数上限：读 <root>/sync-config.json 的 max_pages，缺省 18（T-RO 首投）。"""
    path = root / CONFIG_NAME
    if not path.is_file():
        return DEFAULT_MAX_PAGES
    config = json.loads(path.read_text(encoding="utf-8"))
    limit = config.get("max_pages", DEFAULT_MAX_PAGES)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError(f"{CONFIG_NAME} 的 max_pages 必须是正整数")
    return limit


def check(root: Path) -> dict:
    zh = load_document(working_files(root, "zh"))
    en = load_document(working_files(root, "en"))
    warnings = [{"language": lang, **item} for lang, doc in (("zh", zh), ("en", en)) for item in doc.warnings]
    for file in sorted(set(zh.files) ^ set(en.files)):
        warnings.append(warning("file_mismatch", file=file))
    for id in sorted(set(zh.by_id) - set(en.by_id)):
        warnings.append(warning("missing_en_id", id))
    for id in sorted(set(en.by_id) - set(zh.by_id)):
        warnings.append(warning("extra_en_id", id))
    if [p.id for p in zh.paragraphs] != [p.id for p in en.paragraphs] and set(zh.by_id) == set(en.by_id):
        warnings.append(warning("order_mismatch", zh=[p.id for p in zh.paragraphs], en=[p.id for p in en.paragraphs]))
    if (root / "glossary.tsv").is_file():
        terms = glossary(root)
    else:
        terms = []
        warnings.append(warning("missing_glossary", file="glossary.tsv"))
    tables = table_data(root)
    symbols = shared_symbols(root)
    prepared_terms = [
        (chinese, english, note, [term_pattern(p.strip()) for p in re.split(r"\s*\|\s*", english)])
        for chinese, english, note in terms
    ]
    state = read_state(root)
    exemptions = []
    terminology_hints = []
    for id in sorted(set(state.get("retired_ids", [])) & set(zh.by_id)):
        warnings.append(warning("retired_id_reused", id))
    for id, source in zh.by_id.items():
        target = en.by_id.get(id)
        if target is None:
            continue
        if source.file != target.file:
            warnings.append(warning("paragraph_file_mismatch", id, zh=source.file, en=target.file))
        if target.metadata.get("zh") != source.sha:
            warnings.append(warning("zh_drift", id, expected=source.sha, actual=target.metadata.get("zh")))
        old = state.get("paragraphs", {}).get(id)
        exempt = target.metadata.get("en-only")
        if exempt:
            valid = (
                exempt == target.sha
                and bool(target.metadata.get("en-only-reason"))
                and old is not None
                and old["zh"] == source.sha
                and target.metadata.get("zh") == source.sha
            )
            if not valid:
                warnings.append(warning("invalid_en_only", id, expected=target.sha))
            else:
                exemptions.append({"id": id, "sha": target.sha, "reason": target.metadata["en-only-reason"]})
        if old and old["zh"] == source.sha and old["en"] != target.sha and not exempt:
            warnings.append(warning("en_drift", id, expected=old["en"], actual=target.sha))
        ztext = expand_shared(source.body, root, tables=tables)
        etext = expand_shared(target.body, root, tables=tables)
        znums, enums = numbers(ztext), numbers(etext)
        if znums != enums:
            warnings.append(warning("number_mismatch", id, zh=znums, en=enums))
        zrefs, erefs = references(ztext), references(etext)
        if zrefs != erefs:
            warnings.append(warning("reference_mismatch", id, zh=zrefs, en=erefs))
        zvisible, evisible = visible(ztext, symbols), visible(etext, symbols)
        for chinese, english, note, patterns in prepared_terms:
            if visible(chinese) in zvisible and not any(pattern.search(evisible) for pattern in patterns):
                if note.startswith("[泛词]"):
                    terminology_hints.append(warning("terminology_hint", id, chinese=chinese, required=english))
                else:
                    warnings.append(warning("terminology", id, chinese=chinese, required=english))
    return {
        "schema": "paper.zh_en_check.v1",
        "zh_paragraphs": len(zh.paragraphs),
        "en_paragraphs": len(en.paragraphs),
        "baseline": state.get("tag"),
        "warnings": warnings,
        "en_only": exemptions,
        "terminology_hints": terminology_hints,
    }


def changes(root: Path, since: str | None) -> dict:
    old_files = historical_files(root, since) if since else {}
    before = load_document(old_files) if old_files else Document([], [], [])
    after = load_document(working_files(root, "zh"))
    old_ids, new_ids = before.by_id, after.by_id
    tasks = []
    old_common = [p.id for p in before.paragraphs if p.id in new_ids]
    new_common = [p.id for p in after.paragraphs if p.id in old_ids]
    # 只比较共同编号的相对位置，新增/删除不造成后续整篇虚假调序。
    old_positions = {id: i for i, id in enumerate(old_common)}
    new_positions = {id: i for i, id in enumerate(new_common)}
    for p in after.paragraphs:
        kinds = []
        old = old_ids.get(p.id)
        if old is None:
            kinds.append("added")
        else:
            if old.sha != p.sha:
                kinds.append("modified")
            if old_positions[p.id] != new_positions[p.id] or old.file != p.file:
                kinds.append("reordered")
        if kinds:
            tasks.append(
                {
                    "id": p.id,
                    "changes": kinds,
                    "file": p.file,
                    "line": p.line,
                    "zh_sha": p.sha,
                    "zh": p.body,
                    "previous_zh": old.body if old else None,
                }
            )
    for p in before.paragraphs:
        if p.id not in new_ids:
            tasks.append({"id": p.id, "changes": ["deleted"], "file": p.file, "previous_zh": p.body})
    shared = []
    if since:
        repo, relative = repo_location(root)
        prefix = relative + "/" if relative != "." else ""
        commit = git(root, "rev-parse", "--verify", "--end-of-options", since + "^{commit}")
        paths = git(repo, "diff", "--name-only", commit, "--", prefix + "shared", prefix + "glossary.tsv").splitlines()
        paths += git(
            repo, "ls-files", "--others", "--exclude-standard", "--", prefix + "shared", prefix + "glossary.tsv"
        ).splitlines()
        shared = sorted(set(paths))
    return {
        "schema": "paper.zh_en_changes.v1",
        "since": since,
        "tasks": tasks,
        "shared_or_glossary_changes": shared,
        "warnings": [
            {"version": version, **w} for version, doc in (("before", before), ("after", after)) for w in doc.warnings
        ],
    }


def tasks_markdown(result: dict) -> str:
    lines = [
        "# 英文翻译任务",
        "",
        f"同步点：{result['since'] or '首次同步'}",
        "",
        "只改以下编号；新增/修改段写入 zh_sha，调序保持编号，删除号不得复用。",
        "",
        "数学式、ref/cite/label/eqref 原样保留，遵守 glossary.tsv，按最终效果写 IEEE 英文。",
        "",
    ]
    for task in result["tasks"]:
        lines += [f"## {task['id']} ({', '.join(task['changes'])})", "", f"文件：`{task['file']}`", ""]
        if "zh" in task:
            lines += [
                f"英文编号行：`%%¶ {task['id']} zh={task['zh_sha']}`",
                "",
                "```tex",
                task["zh"].rstrip(),
                "```",
                "",
            ]
        if task.get("previous_zh") is not None:
            lines += ["原中文：", "", "```tex", task["previous_zh"].rstrip(), "```", ""]
    if result["shared_or_glossary_changes"]:
        lines += [
            "## 共用内容/术语表变化（检查所有受影响段落）",
            "",
            *[f"- `{p}`" for p in result["shared_or_glossary_changes"]],
            "",
        ]
    if result["warnings"]:
        lines += ["## 告警", "", *[f"- {json.dumps(w, ensure_ascii=False)}" for w in result["warnings"]], ""]
    return "\n".join(lines)


def pdf_pages(path: Path) -> int:
    """读取 TeX 常用 PDF 的页树；支持 Flate 对象流，不能解析时失败关闭。

    不依赖 pdfinfo/pypdf，也不以 PDF 文本中的数字估计页数。
    """
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"/Encrypt" in data:
        raise ValueError(f"PDF 无效或已加密：{path}")
    # 去掉二进制流，另读取对象流；正文/图片中 /Type /Page 不计数。
    objects = []
    for match in re.finditer(rb"\b\d+\s+\d+\s+obj\b(.*?)\bendobj\b", data, re.S):
        obj = match[1]
        head = obj.split(b"stream", 1)[0]
        objects.append(head)
        if b"/ObjStm" in head:
            # 压缩字节本身可能以 CR/LF 结尾，不能把它当换行分隔符吞掉。
            # zlib 会在完整压缩流末尾停止，忽略 endstream 前的排版换行。
            stream = re.search(rb"stream\r?\n(.*?)endstream", obj, re.S)
            if stream is None or b"/FlateDecode" not in head:
                raise ValueError(f"不支持的 PDF 对象流：{path}")
            try:
                objects.append(zlib.decompress(stream[1]))
            except zlib.error as exc:
                raise ValueError(f"PDF 对象流损坏：{path}") from exc
    combined = b"\n".join(objects)
    pages = len(re.findall(rb"/Type\s*/Page\b", combined))
    counts = []
    for body in re.findall(rb"<<[^<>]*?/Type\s*/Pages\b[^<>]*?>>", combined, re.S):
        match = re.search(rb"/Count\s+(\d+)\b", body)
        if match:
            counts.append(int(match[1]))
    if not pages or not counts or max(counts) != pages:
        raise ValueError(f"无法可靠读取 PDF 页树（不支持/损坏/增量更新）：{path}")
    return pages


def pdf_details(root: Path, zh_pdf: str, en_pdf: str) -> tuple[dict, list[dict]]:
    result = {}
    warnings = []
    limit = max_pages(root)
    for language, name in (("zh", zh_pdf), ("en", en_pdf)):
        path = Path(name)
        path = path if path.is_absolute() else root / path
        try:
            pages = pdf_pages(path)
            result[language] = {
                "path": str(path),
                "pages": pages,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            if language == "en" and pages > limit:
                warnings.append(
                    warning("page_limit", pages=pages, limit=limit, scope="英文正文含参考文献；不含独立 supplementary.pdf")
                )
        except (OSError, ValueError) as exc:
            result[language] = {"path": str(path), "pages": None}
            warnings.append(warning("pdf_unavailable", language=language, detail=str(exc)))
    return result, warnings


def latest_tag(root: Path) -> str | None:
    tags = git(root, "tag", "--list", "zh-en-sync/*").splitlines()
    tags = [t for t in tags if re.fullmatch(r"zh-en-sync/\d+", t)]
    # 同仓多 worktree 的 tag 是共享的，基线取本稿记录，不能取别的稿的最大编号。
    state = read_state(root)
    if state.get("tag"):
        return state["tag"]
    for tag in sorted(tags, key=lambda t: int(t.rsplit("/", 1)[1]), reverse=True):
        if git(root, "merge-base", "HEAD", tag) == git(root, "rev-parse", tag + "^{commit}"):
            return tag
    return None


def report(root: Path, since: str | None, zh_pdf: str, en_pdf: str) -> dict:
    result = check(root)
    result["changes"] = changes(root, since if since is not None else latest_tag(root))
    result["pdfs"], found = pdf_details(root, zh_pdf, en_pdf)
    result["page_limit"] = max_pages(root)
    result["warnings"] += found + result["changes"]["warnings"]
    zh, en = load_document(working_files(root, "zh")), load_document(working_files(root, "en"))
    result["pairs"] = [
        {"id": id, "zh": zh.by_id[id].body if id in zh.by_id else "", "en": en.by_id[id].body if id in en.by_id else ""}
        for id in dict.fromkeys([p.id for p in zh.paragraphs + en.paragraphs])
    ]
    return result


def report_markdown(result: dict) -> str:
    limit = result.get("page_limit", DEFAULT_MAX_PAGES)
    lines = [
        "# 中英逐段对照报告",
        "",
        f"告警数：{len(result['warnings'])}",
        "",
        f"英文页数包含正文和参考文献；独立 supplementary.pdf 不计入 {limit} 页上限。",
        "",
        "| PDF | 页数 | 路径 |",
        "| --- | ---: | --- |",
    ]
    for language, detail in result["pdfs"].items():
        pages = detail["pages"] if detail["pages"] is not None else "未知（门禁未通过）"
        lines.append(f"| {language} | {pages} | {detail['path']} |")
    lines += ["", "## 本轮变更", ""]
    lines += [f"- `{task['id']}`: {', '.join(task['changes'])}" for task in result["changes"]["tasks"]] or [
        "无中文段落变更。"
    ]
    if result["changes"]["shared_or_glossary_changes"]:
        lines += ["", "共享内容/术语表：", "", *[f"- `{p}`" for p in result["changes"]["shared_or_glossary_changes"]]]
    lines += ["", "## 告警", ""]
    lines += [f"- {json.dumps(w, ensure_ascii=False)}" for w in result["warnings"]] or ["无。"]
    hints = result.get("terminology_hints", [])
    lines += ["", "## 泛词提示（不计门禁告警）", ""]
    lines += [f"- {json.dumps(h, ensure_ascii=False)}" for h in hints] or ["无。"]
    if any(w["code"] == "page_limit" for w in result["warnings"]):
        lines += [
            "",
            f"英文超过 {limit} 页：提出删减重复解释、缩短图表说明、转移方法细节至补充材料的具体方案，交用户决定；"
            "先回写中文，随后重译。不得缩字号/页边距掩盖超页。",
        ]
    lines += [
        "",
        "## 仅英文润色记录",
        "",
        json.dumps(result["en_only"], ensure_ascii=False),
        "",
        "## 逐段对照",
        "",
        "| 编号 | 中文 | 英文 |",
        "| --- | --- | --- |",
    ]
    for pair in result["pairs"]:
        cells = [html.escape(pair[key]).replace("|", "&#124;").replace("\n", "<br>") for key in ("id", "zh", "en")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def recorded_tags(repo: Path, root: Path) -> list[str]:
    """仓库内各稿件 SYNC.json/SYNC.md 记录过的同步 tag。

    tag 编号全仓共享；某稿记录的 tag 若未存在于本地仓库（例如在迁入前创建），
    也不能再分配给别的稿件，否则该稿的同步基线会指向别人的快照。
    """
    found = set()
    states = {*repo.glob("*/manuscript-ieee/" + STATE_NAME), root / STATE_NAME}
    histories = {*repo.glob("*/manuscript-ieee/SYNC.md"), root / "SYNC.md"}
    for path in sorted(p for p in states if p.is_file()):
        try:
            tag = json.loads(path.read_text(encoding="utf-8")).get("tag")
        except (OSError, ValueError):
            continue
        if isinstance(tag, str):
            found.add(tag)
    for path in sorted(p for p in histories if p.is_file()):
        found.update(re.findall(r"^##\s+(zh-en-sync/\d+)\s*$", path.read_text(encoding="utf-8"), re.M))
    return sorted(t for t in found if re.fullmatch(r"zh-en-sync/\d+", t))


def mark_synced(root: Path, zh_pdf: str, en_pdf: str) -> dict:
    result = check(root)
    pdfs, found = pdf_details(root, zh_pdf, en_pdf)
    result["warnings"] += found
    if result["warnings"]:
        return result
    repo, relative = repo_location(root)
    base = git(root, "rev-parse", "HEAD")
    tags = git(root, "tag", "--list", "zh-en-sync/*").splitlines() + recorded_tags(repo, root)
    sequence = max([int(t.rsplit("/", 1)[1]) for t in tags if re.fullmatch(r"zh-en-sync/\d+", t)] or [0]) + 1
    tag = f"zh-en-sync/{sequence}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    zh, en = load_document(working_files(root, "zh")), load_document(working_files(root, "en"))
    # 保持英文文本，只更新编号行的中文指纹。
    for file, text in working_files(root, "en").items():

        def replace(match: re.Match) -> str:
            id = match[2]
            metadata = re.sub(r"\s+zh=\S+", "", match[3]).rstrip()
            return f"{match[1]}%%¶ {id} zh={zh.by_id[id].sha}{metadata}"

        # \s 会跨行吞掉 TeX 段间空行，必须只匹配编号行的水平缩进。
        updated = re.sub(r"^([ \t]*)%%¶[ \t]+(\S+)([^\n]*)$", replace, text, flags=re.M)
        if updated != text:
            write(root / file, updated)
    state = {
        "schema": "paper.zh_en_sync.v1",
        "tag": tag,
        "utc": now,
        "base_commit": base,
        "fingerprint_rule": "sha1-8: uncomment; collapse whitespace; strip; UTF-8",
        "paragraphs": {id: {"zh": p.sha, "en": en.by_id[id].sha} for id, p in zh.by_id.items()},
        "pdfs": pdfs,
        "en_only": result["en_only"],
    }
    previous = read_state(root)
    state["retired_ids"] = sorted(
        set(previous.get("retired_ids", [])) | (set(previous.get("paragraphs", {})) - set(zh.by_id))
    )
    write(root / STATE_NAME, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    history_path = root / "SYNC.md"
    history = history_path.read_text(encoding="utf-8") if history_path.exists() else "# 中英同步历史\n"
    history += (
        f"\n## {tag}\n\n- UTC：{now}\n- 基础提交：`{base}`\n- 段落：{len(zh.paragraphs)}\n"
        f"- PDF 页数：中文 {pdfs['zh']['pages']}，英文（含参考文献）{pdfs['en']['pages']}\n"
        f"- 门禁：零告警\n- 仅英文润色：{json.dumps(result['en_only'], ensure_ascii=False)}\n"
    )
    write(history_path, history)
    # mark → commit 的指定顺序下，HEAD 尚未包含新译文；tag 必须指向稿件快照。
    # 私有临时 index 不改变用户暂存区/分支，不把 PDF、build 或报告加入 Git。
    with tempfile.TemporaryDirectory(prefix="zh-en-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(repo, "read-tree", base, env=env)
        paths = [
            "main.tex",
            "zh/main.tex",
            *["zh/" + p for p in zh.files if p != "main.tex"],
            *[p for p in en.files if p != "main.tex"],
            "glossary.tsv",
            STATE_NAME,
            "SYNC.md",
        ]
        if (root / CONFIG_NAME).is_file():
            paths.append(CONFIG_NAME)
        paths += [p.relative_to(root).as_posix() for p in (root / "shared").rglob("*") if p.is_file()]
        for base_dir in (root, root / "zh"):
            paths += [
                p.relative_to(root).as_posix()
                for pattern in ("*.tex", "*.bib", "latexmkrc")
                for p in base_dir.glob(pattern)
                if p.is_file()
            ]
        paths = sorted(set(paths))
        prefix = relative + "/" if relative != "." else ""
        git(repo, "add", "--", *[prefix + p for p in paths], env=env)
        tree = git(repo, "write-tree", env=env)
        snapshot = git(repo, "commit-tree", tree, "-p", base, input=f"docs📝(paper): {tag} 稿件同步快照\n")
    git(repo, "tag", "-a", tag, snapshot, "-m", f"中英同步 {now}；英文 {pdfs['en']['pages']} 页；零告警")
    return {**result, "tag": tag, "snapshot_commit": snapshot, "pdfs": pdfs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "changes", "report", "mark-synced"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="manuscript-ieee 根目录")
        if name == "check":
            sub.add_argument("--json", action="store_true", help="标准输出 JSON")
        if name == "changes":
            sub.add_argument("--since", required=True, help="Git tag 或 commit")
            sub.add_argument(
                "--output",
                type=Path,
                default=Path("sync-changes"),
                help="输出前缀（默认 sync-changes），生成 .json 和 .md；同时 stdout JSON",
            )
        if name in ("report", "mark-synced"):
            sub.add_argument("--zh-pdf", default="zh/build/main.pdf")
            sub.add_argument("--en-pdf", default="build/main.pdf")
        if name == "report":
            sub.add_argument("--since", help="默认 SYNC.json 同步点或首次同步")
            sub.add_argument("--output", type=Path, default=Path("sync-report.md"))
            sub.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if args.command == "check":
            result = check(root)
            if not args.json:
                print(
                    f"中文 {result['zh_paragraphs']} 段；英文 {result['en_paragraphs']} 段；"
                    f"告警 {len(result['warnings'])}"
                )
                for item in result["warnings"]:
                    print(json.dumps(item, ensure_ascii=False))
                return int(bool(result["warnings"]))
        elif args.command == "changes":
            result = changes(root, args.since)
            if args.output:
                write(Path(str(args.output) + ".json"), json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                write(Path(str(args.output) + ".md"), tasks_markdown(result))
        elif args.command == "report":
            result = report(root, args.since, args.zh_pdf, args.en_pdf)
            write(args.output, report_markdown(result))
            if args.json_output:
                write(args.json_output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {"report": str(args.output), "warnings": len(result["warnings"]), "pdfs": result["pdfs"]},
                    ensure_ascii=False,
                )
            )
            return int(bool(result["warnings"]))
        else:
            result = mark_synced(root, args.zh_pdf, args.en_pdf)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(bool(result["warnings"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
