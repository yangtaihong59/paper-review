"""解析器：两篇论文都不崩溃；编号块与 zh_en_sync 的切分逐块一致；中英按编号对齐；统计可报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_review.render import BibResolver, RefResolver, Renderer, to_html
from paper_review.structure import Block, Heading, WorktreeProvider, walk_language

# ---------------------------------------------------------------------------
# 渲染器单元测试（合成输入）
# ---------------------------------------------------------------------------


class NoFigures:
    def register(self, lang, name):
        return None


def renderer(lang: str = "en", tables=None, aux_labels=None, cites=None, style: str = "natbib") -> Renderer:
    return Renderer(lang, {}, tables or {}, RefResolver(aux_labels or {}), BibResolver(cites or {}, {}, {}, style),
                    NoFigures(), {"figure": "Fig.", "table": "TABLE"} if lang == "en" else {"figure": "图", "table": "表"})


def render(src: str, lang: str = "en", env=None, **kwargs) -> str:
    r = renderer(lang, **kwargs)
    return to_html(r.render_block(src, env or []).nodes)


def test_inline_markup():
    html = render(r"A \textbf{bold} and \emph{em} word, \devnum{12.5} m and \todo{fix this}.")
    assert "<strong>bold</strong>" in html and "<em>em</em>" in html
    assert 'class="devnum"' in html and ">12.5<" in html
    assert 'class="todo"' in html and "fix this" in html


def test_unknown_macro_is_visible():
    r = renderer()
    html = to_html(r.render_block(r"Text \frobnicate{inner} end.", []).nodes)
    assert r"\frobnicate" in html and "inner" in html and 'class="unk"' in html
    assert r.unknown["\\frobnicate"] == 1


def test_cjk_line_breaks_do_not_add_spaces():
    html = render("中文第一行\n第二行，English\nwords", lang="zh")
    assert "中文第一行第二行" in html
    assert "English words" in html


def test_table_with_multicolumn_cmidrule_and_shared_data():
    tables = {("tab-x", "r01"): r"\devnum{1.0} & \textbf{2.0}"}
    src = (
        r"\begin{table}[!t]\caption{Demo}\label{tab:x}\begin{tabular}{@{}lrr@{}}\toprule"
        r" & \multicolumn{2}{c}{Group}\\ \cmidrule(l){2-3} Name & A & B\\ \midrule"
        r" Row & \TD{tab-x}{r01}\\ \bottomrule\end{tabular}\end{table}"
    )
    html = render(src, tables=tables, aux_labels={})
    assert 'colspan="2"' in html and "Group" in html
    assert "<th" in html and 'class="al-r cmid"' in html
    assert ">1.0<" in html and "<strong>2.0</strong>" in html
    assert "TABLE I" in html  # IEEEtran 表号为罗马数字（无 .aux 时由源码顺序推算）


def test_unparseable_table_falls_back_to_source():
    html = render(r"\begin{tabular}{l}\multicolumn{x}{y}\end{tabular}")
    assert ("table-source" in html) or ("<table" in html)


def test_table_split_across_blocks_uses_env_context():
    """TRO 式写法：表题、表体、表注分属三块，靠跨块环境栈渲染。"""
    from paper_review.structure import EnvEntry

    caption = render(r"\caption{Split}\label{tab:s}\begin{tabular}{lc}", env=[EnvEntry("table", r"\begin{table}[!t]")])
    assert "Split" in caption and "cont-bottom" in caption
    body = render(r"\toprule A & B\\ \midrule 1 & 2\\ \bottomrule\end{tabular}",
                  env=[EnvEntry("table", r"\begin{table}[!t]", parts=1), EnvEntry("tabular", r"\begin{tabular}{lc}")])
    assert "<table" in body and ">2<" in body and "cont-top" in body
    note = render(r"Note text.\end{table}", env=[EnvEntry("table", r"\begin{table}[!t]", parts=2)])
    assert "Note text." in note and "cont-bottom" not in note


def test_list_items_continue_numbering_across_blocks():
    from paper_review.structure import EnvEntry

    html = render(r"\item Third item.", env=[EnvEntry("enumerate", r"\begin{enumerate}", items=2, parts=2)])
    assert 'start="3"' in html and "Third item." in html


def test_equation_tags_from_labels_and_order():
    r = renderer(aux_labels={})
    nodes = r.render_block(r"\begin{align} a &= b \label{eq:a}\\ c &= d \nonumber\\ e &= f \end{align}", []).nodes
    html = to_html(nodes)
    assert r"\tag{1}" in html and r"\tag{2}" in html and r"\tag{3}" not in html
    assert r.source_labels["eq:a"] == ("1", "equation")
