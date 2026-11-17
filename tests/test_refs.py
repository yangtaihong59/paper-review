"""\\ref/\\eqref 取各自语言 .aux 的真实编号；\\citep/\\citet 取 .aux/.bbl 的编号与标题。"""

from __future__ import annotations

import html as htmlib
import re

import pytest

from paper_review.auxdata import CiteInfo, BibEntry, LabelInfo, parse_aux, parse_bbl
from paper_review.render import BibResolver, RefResolver, Renderer, to_html

class NoFigures:
    def register(self, lang, name):
        return None


def make(style="natbib", labels=None, cites=None, entries=None, lang="en"):
    return Renderer(lang, {}, {}, RefResolver(labels or {}), BibResolver(cites or {}, entries or {}, {}, style),
                    NoFigures(), {"figure": "Fig.", "table": "TABLE"})


def test_cite_styles_and_unresolved():
    cites = {k: CiteInfo(str(n), "2020", f"Auth{n}") for n, k in enumerate(["a", "b", "c", "d", "e"], start=1)}
    natbib = to_html(make("natbib", cites=cites).render_block(r"\citep{c,a,b,e} and \citet{d}", []).nodes)
    assert "–" in natbib and natbib.count("[") >= 2 and "Auth4" in natbib
    ieee = to_html(make("ieee", cites=cites).render_block(r"\cite{a,b,c,e}", []).nodes)
    assert "]–[" in ieee and "], [" in ieee
    r = make("natbib", cites=cites)
    missing = to_html(r.render_block(r"\citep{zzz}", []).nodes)
    assert "unresolved" in missing and r.unresolved_cites["zzz"] == 1


def test_ref_resolution_sources():
    labels = {"fig:a": LabelInfo("3", "2", "", "figure.3"), "sec:b": LabelInfo("IV-B", "5", "", "subsection.4.2")}
    r = make(labels=labels)
    html = to_html(r.render_block(r"Fig.~\ref{fig:a}, Sec.~\ref{sec:b}, \eqref{eq:x}, \ref{nope}", []).nodes)
    assert ">3</a>" in html and ">IV-B</a>" in html
    assert "??" in html and r.unresolved_refs["nope"] == 1
    # 源码推算的编号（.aux 中没有）以 from-source 标出。
    r2 = make(labels={})
    r2.refs.source = {"eq:x": ("7", "equation")}
    html2 = to_html(r2.render_block(r"\eqref{eq:x}", []).nodes)
    assert "(7)" in html2 and "from-source" in html2


def test_bbl_title_and_repeated_authors(tmp_path):
    bbl = tmp_path / "x.bbl"
    bbl.write_text(
        "\\begin{thebibliography}{2}\n"
        "\\bibitem[A(1960a)]{k1}\nR.~E. Kalman, ``On the general theory,'' \\emph{IFAC}, 1960.\n\n"
        "\\bibitem[A(1960b)]{k2}\n------, ``A new approach to {K}alman filtering,'' \\emph{J. Basic Eng.}, 1960.\n"
        "\\end{thebibliography}\n",
        encoding="utf-8",
    )
    entries = parse_bbl(bbl)
    assert entries["k1"].title == "On the general theory"
    assert entries["k2"].title == "A new approach to Kalman filtering"
    assert entries["k2"].text.startswith("R. E. Kalman")
    assert isinstance(entries["k1"], BibEntry)
