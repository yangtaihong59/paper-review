"""小型稿件与独立 Git 仓库，覆盖同步工具门禁和快照边界。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "zh_en_sync.py"
SPEC = importlib.util.spec_from_file_location("zh_en_sync", TOOL)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


def pdf(pages: int, compressed: bool = False) -> bytes:
    # 含页树的最小 PDF；对象流用于覆盖 pdfTeX 的压缩对象布局。
    objects = [f"<< /Type /Pages /Count {pages} /Kids [] >>".encode()]
    objects += [b"<< /Type /Page /Parent 1 0 R >>" for _ in range(pages)]
    if compressed:
        stream = zlib.compress(b"\n".join(objects))
        return (
            b"%PDF-1.5\n1 0 obj\n<< /Type /ObjStm /Filter /FlateDecode >>\nstream\n"
            + stream
            + b"\nendstream\nendobj\n%%EOF\n"
        )
    return (
        b"%PDF-1.4\n"
        + b"\n".join(f"{i} 0 obj\n".encode() + body + b"\nendobj" for i, body in enumerate(objects, 1))
        + b"\n%%EOF\n"
    )


class SyncFixture(unittest.TestCase):
    def setUp(self):
        # 保留 fixture，遵守任务“不删除文件”；只在独立临时仓库打标签。
        parent = os.environ.get("ZH_EN_SYNC_TEST_ROOT")
        self.repo = Path(tempfile.mkdtemp(prefix="zh-en-fixture-", dir=parent))
        self.root = self.repo / "paper"
        self.root.mkdir()
        self.zbody = (
            r"示例算法加速 17×，读取 12\,345 个样本，耗时 $1.2\times10^{9}$。"
            r"\cite{b,a}\ref{fig:a}\label{sec:a}\eqref{eq:a}" + "\n"
        )
        self.ebody = (
            r"Example-method speedup is 17 times with 12,345 samples and $1.2e9$ cost."
            r"\cite{a,b}\ref{fig:a}\label{sec:a}\eqref{eq:a}" + "\n"
        )
        self.write("glossary.tsv", "中文\t英文\t备注\n示例算法\texample-method\t术语\n")
        self.write("main.tex", "\\begin{document}\n\\input{sections/one}\n\\end{document}\n")
        self.write("zh/main.tex", "\\begin{document}\n\\input{sections/one}\n\\end{document}\n")
        self.set_pair()
        self.write("build/main.pdf", pdf(17))
        self.write("zh/build/main.pdf", pdf(16, compressed=True))
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.run_git(
            "add",
            "--",
            "paper/main.tex",
            "paper/zh/main.tex",
            "paper/sections",
            "paper/zh/sections",
            "paper/glossary.tsv",
        )
        self.run_git("commit", "-qm", "fixture baseline")

    def write(self, relative: str, data: str | bytes):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")

    def run_git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), args[0], "--root", str(self.root), *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.repo,
        )

    def set_pair(self, zbody: str | None = None, ebody: str | None = None, metadata: str = ""):
        zbody = self.zbody if zbody is None else zbody
        ebody = self.ebody if ebody is None else ebody
        self.write("zh/sections/one.tex", f"%%¶ intro-01\n{zbody}")
        self.write("sections/one.tex", f"%%¶ intro-01 zh={sync.fingerprint(zbody)}{metadata}\n{ebody}")

    def codes(self) -> set[str]:
        return {w["code"] for w in sync.check(self.root)["warnings"]}

    def test_check_clean_and_cli_exit_codes(self):
        result = self.cli("check", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["warnings"], [])
        self.write("glossary.tsv", "invalid row\n")
        self.assertEqual(self.cli("check").returncode, 2)
        self.assertEqual(self.cli("changes", "--since", "not-a-revision").returncode, 2)

    def test_zh_drift_and_number_mismatch(self):
        self.write("zh/sections/one.tex", "%%¶ intro-01\n" + self.zbody.replace("17", "18"))
        self.assertTrue({"zh_drift", "number_mismatch"} <= self.codes())
        self.assertEqual(self.cli("check").returncode, 1)

    def test_missing_extra_duplicate_and_unmarked(self):
        self.write(
            "sections/one.tex",
            "%%¶ extra-01 zh=12345678\nUnnumbered text.\n\nAnother paragraph.\n%%¶ extra-01\nRepeat.\n",
        )
        self.assertTrue({"missing_en_id", "extra_en_id", "duplicate_id", "missing_marker"} <= self.codes())

    def test_reference_kinds_and_glossary_boundaries(self):
        for command in ("cite", "ref", "label", "eqref"):
            with self.subTest(command=command):
                self.set_pair(ebody=self.ebody.replace("\\" + command + "{", "\\" + command + "{other,"))
                self.assertIn("reference_mismatch", self.codes())
        self.set_pair(ebody=self.ebody.replace("Example-method", "Not-example-methodism"))
        self.assertIn("terminology", self.codes())

    def test_number_normalization(self):
        self.assertEqual(sync.numbers(r"17× 12\,345 $1.2\times10^{9}$"), sync.numbers("17 times 12,345 1.2e9"))
        self.assertEqual(sync.numbers(r"2\,\mathrm{km} 5\,\mathrm{ms} 50\%"), sync.numbers("2000 m 0.005 s 0.5"))
        self.assertEqual(sync.numbers(r"\cite{smith2024}\label{eq:99} G3 v2 P95"), [])
        self.assertEqual(sync.numbers("1200--1560"), sync.numbers("1200–1560"))
        self.assertEqual(sync.numbers("-0 0.00 ０"), ["0"])
        self.assertEqual(sync.numbers("加速17倍，范围2千米"), sync.numbers("17 times, 2000 m"))
        self.assertEqual(sync.numbers("1.5-km"), sync.numbers("1500 m"))
        self.assertEqual(sync.numbers(r"2\,\mathrm{km}\times4\,\mathrm{km}"), sync.numbers("2000 m by 4000 m"))
        self.assertEqual(sync.numbers(r"\SI{36}{km/h}"), sync.numbers("10 m/s"))

    def test_dimension_terms_and_math_delimited_hyphen_units(self):
        self.assertEqual(sync.numbers("3-D localization and 2-D maps"), [])
        self.assertEqual(sync.numbers(r"$3-D$ and $2$ m"), ["2", "3"])
        self.assertEqual(sync.numbers(r"$12.6$-h and $2$-km"), sync.numbers(r"12.6\,\mathrm h and 2000 m"))
        self.assertEqual(sync.numbers(r"$-0.5$-km"), sync.numbers("-500 m"))
        self.set_pair(zbody="三维图上的 3 次重复。\n", ebody="3-D maps with 3 repetitions.\n")
        self.assertEqual(self.codes(), set())
        self.set_pair(zbody="三维图上的三次重复。\n", ebody="3-D maps with 3 repetitions.\n")
        self.assertIn("number_mismatch", self.codes())

    def test_glossary_alternatives_inflections_and_boundaries(self):
        self.write("glossary.tsv", "中文\t英文\t备注\n示例算法\texample-method | advance trajectory\t\n")
        self.set_pair(zbody="示例算法。\n", ebody="Advances trajectories.\n")
        self.assertEqual(self.codes(), set())
        self.set_pair(zbody="示例算法。\n", ebody="Advances trajectoryism.\n")
        self.assertIn("terminology", self.codes())
        for phrase, english in (
            ("IMU sample epoch", "IMU sample epochs"),
            ("gate", "gated"),
            ("gate", "gating"),
            ("admit", "admitted"),
            ("family", "families"),
        ):
            with self.subTest(phrase=phrase, english=english):
                self.write("glossary.tsv", f"中文\t英文\t备注\n术语\t{phrase}\t\n")
                self.set_pair(zbody="术语。\n", ebody=english + ".\n")
                self.assertEqual(self.codes(), set())
        self.write("glossary.tsv", "术语\tfirst | \t\n")
        self.assertEqual(self.cli("check").returncode, 2)

    def test_generic_terms_are_reported_as_hints(self):
        self.write("glossary.tsv", "中文\t英文\t备注\n任务\ttask\t[泛词] 仅提示\n")
        self.set_pair(zbody="任务。\n", ebody="Objective.\n")
        result = sync.check(self.root)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["terminology_hints"][0]["chinese"], "任务")
        self.assertEqual(self.cli("check", "--json").returncode, 0)
        report = sync.report(self.root, "HEAD", "zh/build/main.pdf", "build/main.pdf")
        self.assertIn("泛词提示（不计门禁告警）", sync.report_markdown(report))

    def test_tex_table_heading_word_boundaries(self):
        self.write("glossary.tsv", "中文\t英文\t备注\n重度退化\theavy degradation\t\n")
        self.set_pair(
            zbody=r"\multicolumn{2}{c}{\emph{重度退化}}" + "\n",
            ebody=r"\multicolumn{2}{c}{\emph{Heavy degradation}}" + "\n",
        )
        self.assertEqual(self.codes(), set())

    def test_tex_dash_and_shared_greek_symbol_terms(self):
        self.write("glossary.tsv", "中文\t英文\t备注\n选择与证据分离\tselection–evidence\t\n" + "最小信息特征值\tλ\t\n")
        self.write("shared/macros.tex", r"\newcommand{\lmin}{\lambda_{\min}}" + "\n")
        self.set_pair(zbody="选择与证据分离，最小信息特征值 $\\lmin$。\n", ebody="Selection--evidence and $\\lmin$.\n")
        self.assertEqual(self.codes(), set())
        self.set_pair(zbody="最小信息特征值。\n", ebody=r"$\lambda_{\min}$." + "\n")
        self.assertEqual(self.codes(), set())

    def test_shared_references_and_values(self):
        self.write("shared/table.tex", "17 \\label{tab:shared}\n")
        self.set_pair(zbody="\\input{../shared/table}\n", ebody="\\input{shared/table}\n")
        self.assertEqual(self.codes(), set())
        self.set_pair(ebody="18 \\label{tab:shared}\n")
        self.assertIn("number_mismatch", self.codes())

    def test_real_manuscript_table_and_reference_macros(self):
        self.write(
            "shared/tables/small.tex",
            r"\TDdef{table-one}{r01}{$1.2$ & $\frac{3}{4}$}"
            + "\n"
            + r"\TDdef{table-one}{r02}{$1.3$ & $\frac{3}{4}$}"
            + "\n",
        )
        self.set_pair(
            zbody=r"\TD{table-one}{r01}\mref{sec:one}\placeholderfigure{fig:one}{2in}{图注}{注}" + "\n",
            ebody=r"\TD{table-one}{r01}\mref{sec:one}\placeholderfigure{fig:one}{2in}{Caption}{Note}" + "\n",
        )
        self.assertEqual(self.codes(), set())
        self.set_pair(
            zbody=r"\TD{table-one}{r01}\mref{sec:one}" + "\n", ebody=r"\TD{table-one}{r02}\mref{sec:two}" + "\n"
        )
        self.assertTrue({"number_mismatch", "reference_mismatch"} <= self.codes())

    def test_fingerprint_comment_and_whitespace(self):
        self.assertEqual(sync.fingerprint("中文  $x$ % comment\n\n  done"), sync.fingerprint("中文 $x$ done"))
        self.assertNotEqual(sync.fingerprint(r"50\%"), sync.fingerprint("50"))

    def test_first_sync_before_chinese_exists(self):
        empty_tree = self.run_git("hash-object", "-t", "tree", "--stdin")
        commit = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", empty_tree],
            input="Before Chinese manuscript\n",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        result = self.cli("changes", "--since", commit)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["tasks"][0]["changes"], ["added"])
        self.assertTrue((self.repo / "sync-changes.json").is_file())
        self.assertTrue((self.repo / "sync-changes.md").is_file())

    def test_supplementary_numbering_and_retired_ids(self):
        body = "附录正文。\n"
        self.write("zh/supplementary.tex", "\\begin{document}\n\\input{supp/one}\n\\end{document}\n")
        self.write("supplementary.tex", "\\begin{document}\n\\input{supp/one}\n\\end{document}\n")
        self.write("zh/supp/one.tex", f"%%¶ supp-01\n{body}")
        self.write("supp/one.tex", f"%%¶ supp-01 zh={sync.fingerprint(body)}\nSupplement.\n")
        self.assertEqual(self.codes(), set())
        result = sync.mark_synced(self.root, "zh/build/main.pdf", "build/main.pdf")
        self.assertEqual(result["warnings"], [])
        self.write("zh/supp/one.tex", "")
        self.write("supp/one.tex", "")
        result = sync.mark_synced(self.root, "zh/build/main.pdf", "build/main.pdf")
        self.assertEqual(result["warnings"], [])
        self.write("zh/supp/one.tex", f"%%¶ supp-01\n{body}")
        self.write("supp/one.tex", f"%%¶ supp-01 zh={sync.fingerprint(body)}\nSupplement.\n")
        self.assertIn("retired_id_reused", self.codes())

    def test_english_only_supplement_does_not_require_translation(self):
        self.write("supplementary.tex", "\\begin{document}\n\\input{supp/one}\n\\end{document}\n")
        self.write("supp/one.tex", "%%¶ supp-01\nEnglish-only supplement.\n")
        self.assertEqual(self.codes(), set())

    def test_changes_all_kinds_and_insertion_not_reorder(self):
        old = "%%¶ intro-01\nOne.\n%%¶ intro-02\nTwo.\n%%¶ intro-03\nThree.\n"
        self.write("zh/sections/one.tex", old)
        self.run_git("add", "paper/zh/sections/one.tex")
        self.run_git("commit", "-qm", "before changes")
        self.run_git("tag", "sync-base")
        new = "%%¶ intro-02\nTwo changed.\n%%¶ intro-04\nAdded.\n%%¶ intro-01\nOne.\n"
        self.write("zh/sections/one.tex", new)
        output = self.repo / "tasks"
        result = self.cli("changes", "--since", "sync-base", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tasks = {t["id"]: t["changes"] for t in json.loads(result.stdout)["tasks"]}
        self.assertEqual(
            tasks,
            {
                "intro-02": ["modified", "reordered"],
                "intro-04": ["added"],
                "intro-01": ["reordered"],
                "intro-03": ["deleted"],
            },
        )
        self.assertTrue(Path(str(output) + ".json").is_file())
        self.assertIn("intro-02", Path(str(output) + ".md").read_text())
        self.write("zh/sections/one.tex", "%%¶ intro-04\nAdded.\n" + old)
        tasks = sync.changes(self.root, "sync-base")["tasks"]
        self.assertEqual([t["changes"] for t in tasks], [["added"]])

    def test_input_order_check(self):
        self.write("zh/sections/two.tex", "%%¶ two-01\n中文。\n")
        self.write("sections/two.tex", f"%%¶ two-01 zh={sync.fingerprint('中文。')}\nEnglish.\n")
        self.write("zh/main.tex", "\\input{sections/two}\n\\input{sections/one}\n")
        self.assertIn("order_mismatch", self.codes())

    def test_changes_tracks_shared_working_and_untracked(self):
        self.write("shared/new.tex", "Shared data.\n")
        self.write("glossary.tsv", "中文\t英文\t备注\n示例算法\texample-method\t更改\n")
        result = sync.changes(self.root, "HEAD")
        self.assertEqual(result["shared_or_glossary_changes"], ["paper/glossary.tsv", "paper/shared/new.tex"])

    def test_report_side_by_side_and_pages(self):
        target = self.repo / "comparison.md"
        json_target = self.repo / "comparison.json"
        result = self.cli("report", "--since", "HEAD", "--output", str(target), "--json-output", str(json_target))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(json_target.read_text())
        self.assertEqual(data["pdfs"]["en"]["pages"], 17)
        self.assertEqual(data["pdfs"]["zh"]["pages"], 16)
        self.assertIn("| 编号 | 中文 | 英文 |", target.read_text())
        self.write("build/main.pdf", pdf(19))
        self.assertEqual(self.cli("report", "--output", str(target)).returncode, 1)
        self.assertIn("page_limit", target.read_text())

    def test_missing_and_corrupt_pdf_fail_closed(self):
        target = self.repo / "unavailable.md"
        result = self.cli("report", "--zh-pdf", "missing.pdf", "--output", str(target))
        self.assertEqual(result.returncode, 1)
        self.assertIn("pdf_unavailable", target.read_text())
        self.write("build/main.pdf", b"%PDF-1.4\n/Type /Page\n")
        self.assertEqual(self.cli("mark-synced").returncode, 1)
        self.assertEqual(self.run_git("tag", "--list", "zh-en-sync/*"), "")

    def test_pdf_compressed_stream_binary_carriage_return(self):
        # 真实 pdfTeX 对象流的校验和末字节为 CR，后面紧接 endstream。
        body = b"<< /Type /Pages /Count 1 >>\n<< /Type /Page /Parent 1 0 R >>"
        stream = next(
            candidate
            for padding in range(256)
            if (candidate := zlib.compress(body + b"\n%" + b"a" * padding)).endswith(b"\r")
        )
        for separator in (b"", b"\n", b"\r\n"):
            with self.subTest(separator=separator):
                self.write(
                    "build/main.pdf",
                    b"%PDF-1.5\n1 0 obj\n<< /Type /ObjStm /Filter /FlateDecode /Length "
                    + str(len(stream)).encode()
                    + b" >>\nstream\n"
                    + stream
                    + separator
                    + b"endstream\nendobj\n%%EOF\n",
                )
                self.assertEqual(sync.pdf_pages(self.root / "build/main.pdf"), 1)

    def test_incomplete_draft_report_still_shows_pairs(self):
        root = self.repo / "incomplete"
        (root / "sections").mkdir(parents=True)
        (root / "main.tex").write_text("\\begin{document}\n\\input{sections/one}\n\\end{document}\n")
        (root / "sections/one.tex").write_text("%%¶ intro-01\nEnglish draft.\n")
        result = sync.report(root, None, "missing-zh.pdf", "missing-en.pdf")
        codes = {w["code"] for w in result["warnings"]}
        self.assertTrue({"missing_glossary", "missing_main", "extra_en_id", "pdf_unavailable"} <= codes)
        self.assertEqual(result["pairs"][0]["en"], "English draft.\n")

    def test_mark_snapshot_and_english_drift_exemption(self):
        original_index = self.run_git("write-tree")
        original_head = self.run_git("rev-parse", "HEAD")
        result = self.cli("mark-synced")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["tag"], "zh-en-sync/1")
        self.assertEqual(self.run_git("write-tree"), original_index)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), original_head)
        self.assertIn("zh-en-sync/1", self.run_git("show", "zh-en-sync/1:paper/SYNC.md"))
        self.assertEqual(sync.changes(self.root, "zh-en-sync/1")["tasks"], [])
        self.assertNotIn("paper/build/main.pdf", self.run_git("ls-tree", "-r", "--name-only", "zh-en-sync/1"))
        edited = self.ebody.replace("speedup is", "achieves a speedup of")
        self.set_pair(ebody=edited)
        self.assertIn("en_drift", self.codes())
        self.set_pair(ebody=edited, metadata=f" en-only={sync.fingerprint(edited)} en-only-reason=language-polish")
        self.assertEqual(self.codes(), set())
        self.set_pair(
            ebody=edited + "Further edit.",
            metadata=f" en-only={sync.fingerprint(edited)} en-only-reason=language-polish",
        )
        self.assertIn("invalid_en_only", self.codes())
        self.set_pair(
            ebody=edited.replace("17", "18"),
            metadata=f" en-only={sync.fingerprint(edited.replace('17', '18'))} en-only-reason=polish",
        )
        self.assertIn("number_mismatch", self.codes())

    def test_mark_does_not_bless_drift_or_page_overflow(self):
        self.write("zh/sections/one.tex", "%%¶ intro-01\nUpdated Chinese.\n")
        result = self.cli("mark-synced")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "SYNC.md").exists())
        self.set_pair()
        self.write("build/main.pdf", pdf(19))
        self.assertEqual(self.cli("mark-synced").returncode, 1)
        self.assertEqual(self.run_git("tag", "--list", "zh-en-sync/*"), "")

    def test_mark_preserves_tex_paragraph_breaks_and_language_macros(self):
        second = "后一个段落。\n"
        ztext = f"%%¶ intro-01\n{self.zbody}\n  %%¶ intro-02\n{second}"
        etext = (
            f"%%¶ intro-01 zh={sync.fingerprint(self.zbody)}\n{self.ebody}\n"
            f"  %%¶ intro-02 zh={sync.fingerprint(second)}\nNext paragraph.\n"
        )
        self.write("zh/sections/one.tex", ztext)
        self.write("sections/one.tex", etext)
        self.write("lang-en.tex", r"\newcommand{\Ldesc}{desc.}" + "\n")
        result = self.cli("mark-synced")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.root / "sections/one.tex").read_text(), etext)
        self.assertIn("desc.", self.run_git("show", "zh-en-sync/1:paper/lang-en.tex"))

    def test_page_limit_from_manuscript_config(self):
        # 缺省 18 页（T-RO）；sync-config.json 的 max_pages 改为本稿上限。
        self.write("build/main.pdf", pdf(13))
        self.assertNotIn("page_limit", {w["code"] for w in sync.report(self.root, None, "zh/build/main.pdf", "build/main.pdf")["warnings"]})
        self.write("sync-config.json", json.dumps({"max_pages": 12}))
        result = sync.report(self.root, None, "zh/build/main.pdf", "build/main.pdf")
        self.assertIn({"code": "page_limit", "id": "", "pages": 13, "limit": 12,
                       "scope": "英文正文含参考文献；不含独立 supplementary.pdf"}, result["warnings"])
        self.assertIn("12 页上限", sync.report_markdown(result))
        self.write("build/main.pdf", pdf(12))
        self.assertEqual(self.cli("mark-synced").returncode, 0)
        self.assertIn("paper/sync-config.json", self.run_git("ls-tree", "-r", "--name-only", "zh-en-sync/1"))
        self.write("sync-config.json", json.dumps({"max_pages": 0}))
        self.assertEqual(self.cli("check").returncode, 0)
        self.assertEqual(self.cli("report").returncode, 2)

    def test_tag_number_skips_tags_recorded_by_other_manuscripts(self):
        # 另一篇稿件记录过 zh-en-sync/3，但本地没有该 tag：新编号不能与之重复。
        other = self.repo / "other" / "manuscript-ieee"
        other.mkdir(parents=True)
        (other / "SYNC.json").write_text(json.dumps({"schema": "paper.zh_en_sync.v1", "tag": "zh-en-sync/3"}))
        (other / "SYNC.md").write_text("# 中英同步历史\n\n## zh-en-sync/2\n")
        result = self.cli("mark-synced")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["tag"], "zh-en-sync/4")


if __name__ == "__main__":
    unittest.main()
