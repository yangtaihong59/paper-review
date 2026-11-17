"""组装审阅页面的数据：两种语言的遍历与渲染、按编号对齐、相对基准的改动对比、同步状态与统计。

一切只读：稿件源文件、构建产物与 Git 都不写；生成结果只缓存在内存（rebuild 命令另存快照）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import diff as diffmod
from . import gitinfo, syncstate
from .auxdata import AuxData, parse_aux, parse_bbl, parse_bib_titles
from .config import PaperConfig
from .figures import FigureRegistry
from .macros import Macro, katex_macros, parse_definitions
from .render import BibResolver, BlockRender, Node, RefResolver, Renderer, has_visible_content, to_html, visible_text
from .structure import Block, FileProvider, GitProvider, Heading, LangWalk, WorktreeProvider, preamble_sources, walk_language
from .texutil import TexError, fingerprint, match_brace, uncomment

LANGS = ("zh", "en")


# ---------------------------------------------------------------------------
# 语言环境：导言区宏、共享表体、构建产物
# ---------------------------------------------------------------------------


@dataclass
class LangEnv:
    lang: str
    walk: LangWalk
    macros: dict[str, Macro]
    tables: dict[tuple[str, str], str]
    graphicspath: list[str]
    doc_class: str
    cite_style: str
    names: dict[str, str]
    externals: list[tuple[str, str]]
    supp_macros: dict[str, Macro] = field(default_factory=dict)


def parse_tddefs(text: str) -> dict[tuple[str, str], str]:
    rows = {}
    text = uncomment(text)
    for match in re.finditer(r"\\TDdef\s*\{([^{}]+)\}\s*\{([^{}]+)\}\s*\{", text):
        try:
            end = match_brace(text, match.end() - 1)
        except TexError:
            continue
        rows[(match[1].strip(), match[2].strip())] = text[match.end() : end - 1]
    return rows


def lang_env(provider: FileProvider, lang: str, include_supp: bool) -> LangEnv:
    walk = walk_language(provider, lang, include_supplement=include_supp)
    sources = preamble_sources(provider, lang, walk.preamble)
    macros: dict[str, Macro] = {}
    tables: dict[tuple[str, str], str] = {}
    graphicspath: list[str] = []
    doc_class = "article"
    cite_style = "ieee"
    externals: list[tuple[str, str]] = []
    for rel, text in sources:
        plain = uncomment(text)
        for macro in parse_definitions(plain, rel):
            if macro.name in macros and "providecommand" in plain and macro.source == rel:
                continue
            macros[macro.name] = macro
        tables.update(parse_tddefs(plain))
        gp = re.search(r"\\graphicspath\s*\{((?:\{[^{}]*\})+)\}", plain)
        if gp:
            graphicspath = re.findall(r"\{([^{}]*)\}", gp[1])
        dc = re.search(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}", plain)
        if dc:
            doc_class = dc[1].strip()
        if re.search(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^{}]*\bnatbib\b", plain):
            cite_style = "natbib"
        for m in re.finditer(r"\\externaldocument\s*(?:\[([^\]]*)\])?\s*\{([^{}]+)\}", plain):
            externals.append((m[1] or "", m[2]))
    # 后备：补齐 shared/tables/ 中导言区未引入的表体（与 zh_en_sync 的读取范围一致）。
    shared_files = sorted(
        rel for rel in getattr(provider, "files", []) if rel.startswith("shared/tables/") and rel.endswith(".tex")
    )
    if isinstance(provider, WorktreeProvider):
        shared_files = sorted(
            p.relative_to(provider.root).as_posix() for p in (provider.root / "shared" / "tables").glob("*.tex")
        )
    for rel in shared_files:
        try:
            for key, value in parse_tddefs(provider.read(rel)).items():
                tables.setdefault(key, value)
        except (OSError, FileNotFoundError):
            continue
    if lang == "zh":
        names = {"figure": "图", "table": "表"}
    elif doc_class == "IEEEtran":
        names = {"figure": "Fig.", "table": "TABLE"}
    else:
        names = {"figure": "Figure", "table": "Table"}
    for key, macro_name in (("figure", "figurename"), ("table", "tablename")):
        macro = macros.get(macro_name)
        if macro and macro.safe and "\\" not in macro.body:
            names[key] = macro.body.strip()
    return LangEnv(lang, walk, macros, tables, graphicspath, doc_class, cite_style, names, externals)


def load_aux(manuscript: Path, lang: str, build_dir: str, stem: str, externals: list[tuple[str, str]]) -> tuple[AuxData, dict]:
    base = manuscript / "zh" if lang == "zh" else manuscript
    aux_path = base / build_dir / f"{stem}.aux"
    aux = parse_aux(aux_path)
    for prefix, target in externals:
        external = (base / target).with_suffix(".aux") if not target.endswith(".aux") else base / target
        if external.resolve().is_relative_to(manuscript.resolve()):
            parse_aux(external, aux, prefix=prefix)
    bbl = parse_bbl(base / build_dir / f"{stem}.bbl")
    return aux, bbl


# ---------------------------------------------------------------------------
# 渲染一种语言
# ---------------------------------------------------------------------------


def normalize_title(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).casefold()


@dataclass
class Rendered:
    items: list[dict]
    renderer: Renderer
    supp_renderer: Renderer | None
    title: str


def render_language(env: LangEnv, aux: AuxData, bbl: dict, bib_titles: dict, figures: FigureRegistry | None,
                    supp: tuple[AuxData, dict] | None = None) -> Rendered:
    main_renderer = Renderer(env.lang, env.macros, env.tables, RefResolver(aux.labels),
                             BibResolver(aux.cites, bbl, bib_titles, env.cite_style), figures, env.names, env.doc_class)
    supp_renderer = None
    if supp is not None:
        supp_aux, supp_bbl = supp
        supp_renderer = Renderer(env.lang, env.macros, env.tables, RefResolver(supp_aux.labels),
                                 BibResolver(supp_aux.cites, supp_bbl, bib_titles, env.cite_style), figures,
                                 {"figure": "Figure", "table": "Table"} if env.lang == "en" else env.names, "article")
        # 补充材料的 \thetable = S\arabic{table} 等定义在其导言区，已并入 env.macros。
    toc_by_level: dict[str, list[tuple[str, str]]] = {}
    for level, number, title in aux.toc:
        toc_by_level.setdefault(level, []).append((number, title))

    def run() -> list[dict]:
        out = []
        toc_pos: Counter = Counter()
        title = ""
        for item in env.walk.items:
            renderer = supp_renderer if (item.part if isinstance(item, Block) else "main") == "supp" and supp_renderer else main_renderer
            if isinstance(item, Heading):
                aux_number = None
                if not item.starred and renderer is main_renderer:
                    entries = toc_by_level.get(item.level, [])
                    index = toc_pos[item.level]
                    toc_pos[item.level] += 1
                    if index < len(entries):
                        aux_number = entries[index][0]
                        aux_title = entries[index][1]
                    else:
                        aux_title = None
                else:
                    aux_title = None
                info = renderer.render_heading(item.level, item.starred, item.title, item.label, aux_number)
                if aux_title is not None and normalize_title(aux_title) != normalize_title(info["text"]):
                    renderer.counts["toc_mismatch"] += 1
                    info["number"] = info["source_number"]
                out.append({"type": "heading", "item": item, "info": info,
                            "tokens": diffmod.tokens(info["nodes"], env.lang)})
                continue
            rendered = renderer.render_block(item.content, item.env)
            nodes = rendered.nodes
            if "title" in rendered.flags and not title:
                title = visible_text(nodes).strip()
            out.append({"type": "block", "item": item, "nodes": nodes, "render": rendered,
                        "html": to_html(nodes), "tokens": diffmod.tokens(nodes, env.lang),
                        "visible": has_visible_content(nodes)})
        out.append({"type": "_title", "title": title})
        return out

    # 第一遍收集源码推算的标签编号（供 .aux 缺失的标签使用），第二遍正式渲染。
    run()
    for renderer in filter(None, (main_renderer, supp_renderer)):
        renderer.refs.source = dict(renderer.source_labels)
        renderer.reset()
        renderer.unknown.clear()
        renderer.unknown_envs.clear()
        renderer.unresolved_refs.clear()
        renderer.unresolved_cites.clear()
        renderer.ref_mismatch.clear()
        renderer.table_fallbacks.clear()
        renderer.warnings.clear()
        renderer.counts.clear()
        renderer.bib.used.clear()
    items = run()
    title = items.pop()["title"]
    return Rendered(items, main_renderer, supp_renderer, title)


# ---------------------------------------------------------------------------
# 对齐
# ---------------------------------------------------------------------------


def item_keys(items: list[dict]) -> list[str]:
    """块按编号；标题按其后第一个编号块（中英标签名可能不同）；独立编号的标题按编号。"""
    keys: list[str | None] = [None] * len(items)
    next_block: str | None = None
    for index in range(len(items) - 1, -1, -1):
        entry = items[index]
        item = entry["item"]
        if entry["type"] == "block":
            keys[index] = "b:" + item.id
            next_block = item.id
        elif item.block_id:
            keys[index] = "b:" + item.block_id
            next_block = item.block_id
        else:
            anchor = next_block or (item.label or f"end{index}")
            keys[index] = f"h:{item.level}:{anchor}"
    seen: Counter = Counter()
    result = []
    for key in keys:
        seen[key] += 1
        result.append(key if seen[key] == 1 else f"{key}#{seen[key]}")
    return result


def merge(zh_keys: list[str], en_keys: list[str]) -> list[str]:
    """以中文顺序为主；仅英文的条目插在其前一个共同条目之后。"""
    zh_set = set(zh_keys)
    anchored: dict[str | None, list[str]] = {}
    previous: str | None = None
    for key in en_keys:
        if key in zh_set:
            previous = key
        else:
            anchored.setdefault(previous, []).append(key)
    order = list(anchored.get(None, []))
    for key in zh_keys:
        order.append(key)
        order.extend(anchored.get(key, []))
    return order


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


@dataclass
class Built:
    doc: dict
    signature: str
    built_at: float
    stats: dict


def manuscript_prefix(paper: PaperConfig, repo: Path) -> str:
    return paper.manuscript.relative_to(repo).as_posix()


def default_base(paper: PaperConfig, repo: Path, state_dir: Path | None = None) -> tuple[str, str | None, str]:
    """默认对比基准（“本轮修改”相对谁）。

    1. config.toml 中该论文的 default_base：last-round 取最近已提交轮次的同步 tag，
       tag 不存在时用该轮 commit_sha；其他值作为显式 ref；
    2. 否则取本稿 SYNC.md/SYNC.json 记录、且本地存在的同步 tag 中的倒数第二个，即“本轮修订前”：
       一轮修订以同步 tag 收尾，审阅时要看的是相对上一次同步的改动；
    3. 只有一个同步 tag 时用它，一个都没有时用 HEAD。
    """
    if paper.default_base == "last-round":
        # 不使用 Store：其初始化和轮次查询可能建表、迁移或创建新轮次。
        path = (state_dir if state_dir is not None else paper.state_dir()) / "annotations.sqlite"
        row = None
        if not path.is_file():
            note = "last-round：批注库不存在；退回自动规则；"
        else:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT round, sync_tag, commit_sha FROM rounds "
                        "WHERE paper = ? AND submitted_at IS NOT NULL AND submitted_at != '' "
                        "ORDER BY round DESC LIMIT 1", (paper.name,)
                    ).fetchone()
                note = "last-round：无已提交轮次；退回自动规则；"
            except sqlite3.Error as exc:
                note = f"last-round：读取批注库失败（{exc}）；退回自动规则；"
        if row is not None:
            number, tag, sha = row
            commit = gitinfo.resolve(repo, f"refs/tags/{tag}") if tag else None
            if commit:
                return tag, commit, ""
            commit = gitinfo.resolve(repo, sha) if sha else None
            reason = f"第 {number} 轮同步 tag {tag or '未记录'} 在本地不可用"
            if commit:
                return sha, commit, f"{reason}，改用该轮 commit_sha"
            note = f"last-round：{reason}，commit_sha {sha or '未记录'} 也不可用；退回自动规则；"
    elif paper.default_base:
        commit = gitinfo.resolve(repo, paper.default_base)
        if commit:
            return paper.default_base, commit, ""
        note = f"config.toml 指定的基准 {paper.default_base} 无法解析；"
    else:
        note = ""
    tags = [(tag, gitinfo.resolve(repo, tag)) for tag in gitinfo.sync_tags_for(paper.manuscript)]
    available = [(tag, commit) for tag, commit in tags if commit]
    missing = [tag for tag, commit in tags if not commit]
    if len(available) >= 2:
        tag, commit = available[-2]
        return tag, commit, note
    if available:
        tag, commit = available[-1]
        return tag, commit, note
    if missing:
        note += f"本稿同步点 {missing[-1]} 在本地不存在，改用 HEAD"
    return "HEAD", gitinfo.resolve(repo, "HEAD"), note


def build(paper: PaperConfig, repo: Path, figures: FigureRegistry, base_ref: str | None = None,
          include_supp: bool = False, state_dir: Path | None = None) -> Built:
    started = time.time()
    provider = WorktreeProvider(paper.manuscript)
    envs = {lang: lang_env(provider, lang, include_supp) for lang in LANGS}
    figures.graphicspaths = {lang: envs[lang].graphicspath for lang in LANGS}
    bib_titles = parse_bib_titles(sorted(paper.manuscript.glob("*.bib")) + sorted(paper.dir.glob("manuscript/*.bib")))
    rendered: dict[str, Rendered] = {}
    aux_info = {}
    for lang in LANGS:
        aux, bbl = load_aux(paper.manuscript, lang, paper.build_dir, "main", envs[lang].externals)
        supp = None
        if include_supp:
            supp = load_aux(paper.manuscript, lang, paper.build_dir, "supplementary", [])
            # 补充材料引用正文标签（M- 前缀）时，正文 aux 以 xr 方式并入。
            parse_aux((paper.manuscript / ("zh" if lang == "zh" else "") / paper.build_dir / "main.aux"), supp[0],
                      prefix="M-")
        rendered[lang] = render_language(envs[lang], aux, bbl, bib_titles, figures, supp)
        aux_info[lang] = {"path": aux.paths[0] if aux.paths else None, "mtime": aux.mtime,
                          "labels": len(aux.labels), "cites": len(aux.cites)}

    # 同步状态。
    check = syncstate.run_check(paper.sync_tool, paper.manuscript)
    per_id, global_sync = syncstate.states(check)

    # 对比基准。
    base_name, base_commit, base_note = default_base(paper, repo, state_dir)
    if base_ref:
        commit = gitinfo.resolve(repo, base_ref)
        if commit is None:
            base_note = f"无法解析基准 {base_ref}，改用默认基准 {base_name}"
        else:
            base_name, base_commit, base_note = base_ref, commit, ""
    base_rendered: dict[str, Rendered] = {}
    base_error = ""
    if base_commit:
        try:
            gp = GitProvider(repo, base_commit, manuscript_prefix(paper, repo))
            for lang in LANGS:
                try:
                    benv = lang_env(gp, lang, include_supp)
                except FileNotFoundError:
                    continue
                aux, bbl = load_aux(paper.manuscript, lang, paper.build_dir, "main", benv.externals)
                base_rendered[lang] = render_language(benv, aux, bbl, bib_titles, None, None)
        except Exception as exc:  # noqa: BLE001 - 基准不可用时页面仍可阅读
            base_error = f"读取基准 {base_name} 失败：{exc}"

    doc = assemble(paper, repo, envs, rendered, base_rendered, per_id, global_sync, check)
    doc["header"]["base"] = {
        "ref": base_name,
        "commit": base_commit,
        "short": (base_commit or "")[:7],
        "note": base_note or base_error,
        "requested": base_ref,
    }
    doc["header"]["build"]["aux"] = aux_info
    doc["header"]["build"]["seconds"] = round(time.time() - started, 2)
    doc["options"] = {"supp": include_supp}
    return Built(doc, "", started, doc["stats"])


def block_payload(entry: dict, lang: str) -> dict:
    item = entry["item"]
    return {
        "html": entry["html"],
        "file": ("zh/" if lang == "zh" else "") + item.file if not item.file.startswith("zh/") else item.file,
        "line": item.line,
        "sha": item.sha,
        "labels": entry["render"].labels,
        "visible": entry["visible"],
    }


def assemble(paper, repo, envs, rendered, base_rendered, per_id, global_sync, check) -> dict:
    entries = {lang: rendered[lang].items for lang in LANGS}
    keys = {lang: item_keys(entries[lang]) for lang in LANGS}
    by_key = {lang: dict(zip(keys[lang], entries[lang])) for lang in LANGS}
    order = merge(keys["zh"], keys["en"])

    base_by_key: dict[str, dict] = {}
    base_order: list[str] = []
    for lang in LANGS:
        if lang in base_rendered:
            bkeys = item_keys(base_rendered[lang].items)
            base_by_key[lang] = dict(zip(bkeys, base_rendered[lang].items))
            for key in bkeys:
                if key not in base_order:
                    base_order.append(key)
    has_base = bool(base_rendered)

    rows: list[dict] = []
    section_index = -1
    for key in order:
        zh = by_key["zh"].get(key)
        en = by_key["en"].get(key)
        if zh and en and zh["type"] != en["type"]:
            # 同一编号在一种语言是独立编号的标题、另一种是正文块（结构尚未同步）：标题按行内标题块显示。
            zh, en = [heading_as_block(e) if e["type"] == "heading" else e for e in (zh, en)]
        first = zh or en
        if first["type"] == "heading":
            row = heading_row(key, zh, en, base_by_key, has_base, per_id)
            if row["level"] in ("part", "section", "subsection", "subsubsection"):
                section_index = len(rows)
            rows.append(row)
            continue
        row = block_row(key, zh, en, base_by_key, has_base, per_id)
        row["section"] = section_index
        rows.append(row)

    # 基准中有、当前已删除的段落：放在其前一个仍存在的条目之后。
    if has_base:
        present = set(order)
        positions = {row["key"]: i for i, row in enumerate(rows)}
        inserts: dict[int, list[dict]] = {}
        previous_present = None
        for key in base_order:
            if key in present:
                previous_present = key
                continue
            if not key.startswith("b:"):
                continue
            ghost = {"type": "block", "key": key, "id": key[2:], "deleted": True, "kind": "deleted",
                     "zh": None, "en": None, "diff": {}, "changed": {}, "sync": {"state": "deleted", "warnings": []},
                     "media": [], "media_shared": False, "wide": False, "unmarked": key.startswith("b:~")}
            for lang in LANGS:
                old = base_by_key.get(lang, {}).get(key)
                if old:
                    ghost["diff"][lang] = diffmod.whole(old["tokens"], "deleted")
            anchor_index = positions.get(previous_present, -1)
            inserts.setdefault(anchor_index, []).append(ghost)
        if inserts:
            new_rows = list(inserts.get(-1, []))
            for index, row in enumerate(rows):
                new_rows.append(row)
                new_rows.extend(inserts.get(index, []))
            rows = new_rows
            # 重新计算 section 索引。
            section_index = -1
            for index, row in enumerate(rows):
                if row["type"] == "heading" and row["level"] in ("part", "section", "subsection", "subsubsection"):
                    section_index = index
                elif row["type"] == "block":
                    row["section"] = section_index

    # 统计。
    ids = {lang: [e["item"].id for e in entries[lang] if e["type"] == "block" and not e["item"].unmarked]
           + [e["item"].block_id for e in entries[lang] if e["type"] == "heading" and e["item"].block_id]
           for lang in LANGS}
    zh_ids, en_ids = set(ids["zh"]), set(ids["en"])
    unknown = {lang: dict(rendered[lang].renderer.unknown.most_common()) for lang in LANGS}
    for lang in LANGS:
        if rendered[lang].supp_renderer:
            for name, count in rendered[lang].supp_renderer.unknown.items():
                unknown[lang][name] = unknown[lang].get(name, 0) + count
    renderer_stats = {}
    for lang in LANGS:
        r = rendered[lang].renderer
        renderer_stats[lang] = {
            "unknown_macros": sum(r.unknown.values()),
            "unknown_macro_names": dict(r.unknown.most_common()),
            "unknown_envs": dict(r.unknown_envs),
            "unresolved_refs": sorted(r.unresolved_refs),
            "unresolved_cites": sorted(r.unresolved_cites),
            "refs_from_source": sorted(k for k, v in r.refs.source.items() if k not in r.refs.aux),
            "ref_mismatch": {k: {"aux": v[0], "source": v[1]} for k, v in r.ref_mismatch.items()},
            "table_fallbacks": len(r.table_fallbacks),
            "toc_mismatch": r.counts.get("toc_mismatch", 0),
            "refs": r.counts.get("refs", 0),
            "cites": r.counts.get("cites", 0),
            "tables": r.counts.get("tables", 0),
            "warnings": r.warnings[:50],
        }
    block_rows = [r for r in rows if r["type"] == "block" and not r.get("deleted")]
    doc_stats = {
        "zh_blocks": len(zh_ids),
        "en_blocks": len(en_ids),
        "aligned": len(zh_ids & en_ids),
        "zh_only": sorted(zh_ids - en_ids),
        "en_only": sorted(en_ids - zh_ids),
        "unmarked": {lang: sum(1 for e in entries[lang] if e["type"] == "block" and e["item"].unmarked
                               and e["item"].part == "main") for lang in LANGS},
        "supplement_blocks": {lang: sum(1 for e in entries[lang] if e["type"] == "block" and e["item"].part == "supp")
                              for lang in LANGS},
        "headings": {lang: sum(1 for e in entries[lang] if e["type"] == "heading") for lang in LANGS},
        "figures": sum(1 for r in block_rows if r["kind"] == "figure"),
        "tables": sum(1 for r in block_rows if r["kind"] == "table"),
        "changed": sum(1 for r in rows if r.get("changed") and any(r["changed"].values())),
        "deleted": sum(1 for r in rows if r.get("deleted")),
        "render": renderer_stats,
        "walk_warnings": {lang: envs[lang].walk.warnings[:50] for lang in LANGS},
        "sync_check": {
            "ok": check.get("ok"),
            "exit": check.get("exit"),
            "error": check.get("error"),
            "zh_paragraphs": (check.get("result") or {}).get("zh_paragraphs"),
            "en_paragraphs": (check.get("result") or {}).get("en_paragraphs"),
            "warnings": Counter(w.get("code") for w in (check.get("result") or {}).get("warnings", [])),
        },
    }
    katex = {lang: katex_macros(envs[lang].macros) for lang in LANGS}
    for lang in LANGS:
        katex[lang]["\\devnum"] = "\\htmlClass{devnum}{#1}"
        katex[lang]["\\todo"] = "\\htmlClass{todo}{\\text{TODO }#1}"
        for name in ("revadd", "revdel", "revdelblk"):
            katex[lang]["\\" + name] = f"\\htmlClass{{{name}}}{{#1}}"
    header = header_info(paper, repo, rendered, check)
    toc = [
        {"index": i, "key": r["key"], "level": r["level"], "starred": r.get("starred", False),
         "number": (r["zh"] or r["en"] or {}).get("number", ""),
         "zh": (r["zh"] or {}).get("text", ""), "en": (r["en"] or {}).get("text", "")}
        for i, r in enumerate(rows)
        if r["type"] == "heading" and r["level"] in ("section", "subsection", "subsubsection", "part")
    ]
    bib = {lang: rendered[lang].renderer.bib.table() for lang in LANGS}
    for lang in LANGS:
        if rendered[lang].supp_renderer:
            bib[lang + "-supp"] = rendered[lang].supp_renderer.bib.table()
    return {
        "paper": {"id": paper.id, "dir": paper.name},
        "header": header,
        "rows": rows,
        "toc": toc,
        "bib": bib,
        "katex_macros": katex,
        "sync_global": global_sync,
        "stats": doc_stats,
    }


class HeadingBlock:
    """把独立编号的标题当作正文块显示时使用的占位条目。"""

    def __init__(self, heading: Heading):
        self.id = heading.block_id
        self.file = heading.file
        self.line = heading.line
        self.raw = getattr(heading, "raw", heading.title)
        self.content = ""
        self.env = []
        self.metadata = getattr(heading, "metadata", {})
        self.unmarked = False
        self.part = "main"

    @property
    def sha(self) -> str:
        return fingerprint(self.raw)


def heading_as_block(entry: dict) -> dict:
    info = entry["info"]
    level = entry["item"].level
    nodes = [Node("div", {"class": f"inline-heading {level}"},
                  ([Node("span", {"class": "hnum"}, [info["number"] + " "])] if info["number"] else []) + info["nodes"])]
    render = BlockRender(nodes, flags={"heading"})
    return {"type": "block", "item": HeadingBlock(entry["item"]), "nodes": nodes, "render": render,
            "html": to_html(nodes), "tokens": entry["tokens"], "visible": True}


def changed_state(key: str, entry: dict | None, lang: str, base_by_key: dict, has_base: bool) -> str | None:
    if not has_base or entry is None:
        return None
    base = base_by_key.get(lang, {}).get(key)
    if base is None:
        return "added"
    return None if diffmod.keys(base["tokens"]) == diffmod.keys(entry["tokens"]) else "modified"


def sync_for(pid: str | None, zh: dict | None, en: dict | None, per_id: dict, unmarked: bool, part: str) -> dict:
    """逐段同步状态：取自 zh_en_sync check --json；本视图（正文）中只有一侧时以单侧为准。

    同步工具在建立了中文补充材料的稿件上会把补充材料一并检查，某编号可能只在正文的一侧、
    却在另一语言的补充材料里出现；此时状态记为仅中文/仅英文，工具给出的告警码保留在 codes 中。
    """
    if part == "supp":
        return {"state": "monolingual", "warnings": []}
    if unmarked or not pid:
        return {"state": "unmarked", "warnings": []}
    entry = per_id.get(pid) or {}
    state = entry.get("state", "aligned")
    if zh is None:
        state = "en-only"
    elif en is None:
        state = "zh-only"
    elif state in ("zh-only", "en-only"):
        state = "aligned"
    return {"state": state, "warnings": entry.get("warnings", []), "codes": entry.get("codes", [])}


def heading_row(key, zh, en, base_by_key, has_base, per_id) -> dict:
    first = zh or en
    item = first["item"]
    out = {"type": "heading", "key": key, "id": item.block_id, "level": item.level, "starred": item.starred}
    for lang, entry in (("zh", zh), ("en", en)):
        if entry is None:
            out[lang] = None
            continue
        info = entry["info"]
        h = entry["item"]
        out[lang] = {"html": info["html"], "text": info["text"], "number": info["number"],
                     "anchor": info["anchor"], "file": ("zh/" if lang == "zh" else "") + h.file, "line": h.line}
    out["changed"] = {lang: changed_state(key, e, lang, base_by_key, has_base) for lang, e in (("zh", zh), ("en", en))}
    if item.block_id:
        out["sync"] = sync_for(item.block_id, zh, en, per_id, False, "main")
    return out


def block_row(key, zh, en, base_by_key, has_base, per_id) -> dict:
    first = zh or en
    item: Block = first["item"]
    flags = set()
    for entry in (zh, en):
        if entry:
            flags |= entry["render"].flags
    if "float-figure" in flags:
        kind = "figure"
    elif "float-table" in flags:
        kind = "table"
    elif "float-part" in flags or any(e.name in ("table", "table*", "figure", "figure*") for e in item.env):
        kind = "float-part"
    elif "title" in flags:
        kind = "title"
    elif "author" in flags:
        kind = "author"
    elif "abstract" in flags or any(e.name == "abstract" for e in item.env):
        kind = "abstract"
    elif "keywords" in flags:
        kind = "keywords"
    elif "list" in flags:
        kind = "item"
    elif "math-display" in flags and not any(
        isinstance(n, object) and getattr(n, "tag", None) == "p" for e in (zh, en) if e for n in e["nodes"]
    ):
        kind = "equation"
    else:
        kind = "para"
    wide = "wide" in flags or any(e.name in ("table*", "figure*") for e in item.env)
    row = {"type": "block", "key": key, "id": item.id, "kind": kind, "wide": wide, "unmarked": item.unmarked,
           "part": item.part}
    media: dict[str, list] = {}
    for lang, entry in (("zh", zh), ("en", en)):
        row[lang] = block_payload(entry, lang) if entry else None
        media[lang] = entry["render"].media if entry else []
    zh_media = [m["id"] for m in media["zh"]]
    en_media = [m["id"] for m in media["en"]]
    row["media"] = media["en"] or media["zh"]
    row["media_shared"] = bool(row["media"]) and (zh_media == en_media or not zh_media or not en_media)
    if not row["media_shared"]:
        row["media"] = []
    row["changed"] = {lang: changed_state(key, e, lang, base_by_key, has_base) for lang, e in (("zh", zh), ("en", en))}
    row["diff"] = {}
    for lang, entry in (("zh", zh), ("en", en)):
        state = row["changed"][lang]
        if not state or entry is None:
            continue
        base = base_by_key.get(lang, {}).get(key)
        if state == "added" or base is None:
            row["diff"][lang] = diffmod.whole(entry["tokens"], "added")
        else:
            row["diff"][lang] = diffmod.diff_html(base["tokens"], entry["tokens"], lang)
    row["sync"] = sync_for(item.id, zh, en, per_id, item.unmarked, item.part)
    return row


def header_info(paper: PaperConfig, repo: Path, rendered: dict, check: dict) -> dict:
    sync_state = {}
    state_path = paper.manuscript / "SYNC.json"
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            sync_state = {"tag": data.get("tag"), "utc": data.get("utc")}
        except ValueError:
            pass
    tag = sync_state.get("tag")
    if tag:
        sync_state["exists"] = bool(gitinfo.resolve(repo, tag))
    prefix = manuscript_prefix(paper, repo)
    pdfs = {}
    for lang in LANGS:
        pdf = (paper.manuscript / ("zh" if lang == "zh" else "") / paper.build_dir / "main.pdf")
        pdfs[lang] = pdf.stat().st_mtime if pdf.is_file() else None
    result = check.get("result") or {}
    return {
        "title": {lang: rendered[lang].title for lang in LANGS},
        "commit": gitinfo.head(repo),
        "dirty": gitinfo.dirty(repo, prefix),
        "sync": {**sync_state, "check_ok": check.get("ok"), "check_exit": check.get("exit"),
                 "check_error": check.get("error"), "warnings": len(result.get("warnings", []))},
        "build": {"rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pdf": pdfs},
    }


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def signature(paper: PaperConfig, repo: Path, figures: FigureRegistry, extra: str,
              state_dir: Path | None = None) -> str:
    """稿件文件 stat、Git HEAD 与默认审阅基准的变化指纹。"""
    parts = [extra, gitinfo.git(repo, "rev-parse", "HEAD", check=False)]
    if paper.default_base == "last-round":
        # 提交批注后，即使稿件与 HEAD 未变，也要更新默认基准及页面版本。
        parts.append(repr(default_base(paper, repo, state_dir)))
    root = paper.manuscript
    dirs = [root, root / "sections", root / "supp", root / "shared", root / "shared" / "tables", root / "zh",
            root / "zh" / "sections", root / "zh" / "supp", root / paper.build_dir, root / "zh" / paper.build_dir]
    for directory in dirs:
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_file() and entry.name.endswith((".tex", ".aux", ".bbl", ".json", ".tsv", ".bib", ".md")):
                        st = entry.stat()
                        parts.append(f"{entry.path}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            continue
    for ref in list(figures.items.values()):
        try:
            st = ref.source.stat()
            parts.append(f"{ref.source}:{st.st_mtime_ns}")
        except OSError:
            continue
    import hashlib

    return hashlib.sha1("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


class PaperState:
    """一篇论文的渲染缓存（按 基准提交 × 是否含补充材料 分别缓存）。"""

    def __init__(self, paper: PaperConfig, repo: Path, state_dir: Path):
        self.paper = paper
        self.repo = repo
        self.state_dir = state_dir
        self.figures = FigureRegistry(paper.id, paper.dir, paper.manuscript, state_dir, {})
        self.cache: dict[tuple, Built] = {}
        self.lock = threading.Lock()

    def get(self, base_ref: str | None = None, include_supp: bool = False, force: bool = False) -> Built:
        with self.lock:
            commit = gitinfo.resolve(self.repo, base_ref) if base_ref else None
            key = (commit or base_ref or "", include_supp)
            sig = signature(self.paper, self.repo, self.figures, f"{key}", self.state_dir)
            cached = self.cache.get(key)
            if cached is not None and cached.signature == sig and not force:
                return cached
            built = build(self.paper, self.repo, self.figures, base_ref, include_supp, self.state_dir)
            built.signature = signature(self.paper, self.repo, self.figures, f"{key}", self.state_dir)
            self.cache[key] = built
            return built

    def current_signature(self, base_ref: str | None = None, include_supp: bool = False) -> str:
        commit = gitinfo.resolve(self.repo, base_ref) if base_ref else None
        key = (commit or base_ref or "", include_supp)
        return signature(self.paper, self.repo, self.figures, f"{key}", self.state_dir)
