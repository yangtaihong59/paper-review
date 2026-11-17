"""用临时 Git 仓库与只供测试写入的批注库验证自动审阅基准。"""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from paper_review import document
from paper_review.config import PaperConfig


@pytest.fixture
def review(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    commits = []
    for n in range(1, 4):
        git("-c", "user.name=Review Test", "-c", "user.email=review@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", f"sync {n}")
        commits.append(git("rev-parse", "HEAD"))
        git("tag", f"zh-en-sync/{n}")
    paper_dir = repo / "test-paper"
    manuscript = paper_dir / "manuscript-ieee"
    manuscript.mkdir(parents=True)
    (manuscript / "SYNC.md").write_text("\n".join(f"## zh-en-sync/{n}" for n in range(1, 4)))
    paper = PaperConfig("test", paper_dir, manuscript, repo / "unused.py", default_base="last-round")
    return paper, repo, commits


def write_rounds(paper, rows, state_dir=None):
    path = (state_dir or paper.state_dir()) / "annotations.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE rounds (paper TEXT, round INTEGER, submitted_at TEXT, "
                     "sync_tag TEXT, commit_sha TEXT)")
        conn.executemany("INSERT INTO rounds VALUES (?, ?, ?, ?, ?)", rows)
    return path


def test_latest_submitted_round_uses_its_tag_read_only(review):
    paper, repo, commits = review
    path = write_rounds(paper, [
        (paper.name, 1, "2026-11-08", "zh-en-sync/2", commits[1]),
        (paper.name, 2, "2026-11-09", "zh-en-sync/1", commits[2]),
        (paper.name, 3, None, "zh-en-sync/3", commits[2]),
        ("other-paper", 99, "2026-11-10", "zh-en-sync/3", commits[2]),
    ])
    before = path.read_bytes()
    assert document.default_base(paper, repo) == ("zh-en-sync/1", commits[0], "")
    assert path.read_bytes() == before


def test_missing_tag_uses_round_commit(review):
    paper, repo, commits = review
    write_rounds(paper, [(paper.name, 1, "2026-11-09", "zh-en-sync/99", commits[0])])
    ref, commit, note = document.default_base(paper, repo)
    assert (ref, commit) == (commits[0], commits[0])
    assert "zh-en-sync/99" in note and "commit_sha" in note


@pytest.mark.parametrize("submitted_at", [None, ""])
def test_no_submitted_round_uses_previous_sync_tag(review, submitted_at):
    paper, repo, commits = review
    write_rounds(paper, [(paper.name, 1, submitted_at, "zh-en-sync/1", commits[0])])
    ref, commit, note = document.default_base(paper, repo)
    assert (ref, commit) == ("zh-en-sync/2", commits[1])
    assert "无已提交轮次" in note and "退回自动规则" in note


@pytest.mark.parametrize("tag,sha", [("zh-en-sync/99", "0" * 40), (None, None)])
def test_unavailable_tag_and_commit_use_old_rule(review, tag, sha):
    paper, repo, commits = review
    write_rounds(paper, [(paper.name, 1, "2026-11-09", tag, sha)])
    ref, commit, note = document.default_base(paper, repo)
    assert (ref, commit) == ("zh-en-sync/2", commits[1])
    assert "commit_sha" in note and "退回自动规则" in note


def test_missing_database_is_not_created(review):
    paper, repo, commits = review
    ref, commit, note = document.default_base(paper, repo)
    assert (ref, commit) == ("zh-en-sync/2", commits[1])
    assert "批注库不存在" in note
    assert not paper.state_dir().exists()


def test_unreadable_database_uses_old_rule(review):
    paper, repo, commits = review
    path = paper.state_dir() / "annotations.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid sqlite database")
    ref, commit, note = document.default_base(paper, repo)
    assert (ref, commit) == ("zh-en-sync/2", commits[1])
    assert "读取批注库失败" in note
    assert path.read_bytes() == b"invalid sqlite database"


def test_state_override_and_submission_invalidate_cached_default(review, tmp_path, monkeypatch):
    paper, repo, commits = review
    write_rounds(paper, [(paper.name, 1, "2026-11-08", "zh-en-sync/3", commits[2])])
    state_dir = tmp_path / "isolated-state"
    path = write_rounds(paper, [(paper.name, 1, "2026-11-08", "zh-en-sync/1", commits[0])], state_dir)
    calls = []

    def build(paper, repo, figures, base_ref, include_supp, state_dir):
        base = document.default_base(paper, repo, state_dir)
        calls.append(base)
        return document.Built({"base": base}, "", 0, {})

    monkeypatch.setattr(document, "build", build)
    state = document.PaperState(paper, repo, state_dir)
    first = state.get()
    assert first.doc["base"] == ("zh-en-sync/1", commits[0], "")
    assert state.get() is first
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO rounds VALUES (?, 2, ?, ?, ?)",
                     (paper.name, "2026-11-09", "zh-en-sync/2", commits[1]))
    assert state.current_signature() != first.signature
    second = state.get()
    assert second.doc["base"] == ("zh-en-sync/2", commits[1], "")
    assert second.signature != first.signature
    assert len(calls) == 2


@pytest.mark.parametrize("configured,expected", [(None, "zh-en-sync/2"), ("zh-en-sync/1", "zh-en-sync/1")])
def test_existing_default_modes_are_unchanged(review, configured, expected):
    paper, repo, commits = review
    paper.default_base = configured
    ref, commit, note = document.default_base(paper, repo)
    assert ref == expected and commit == commits[int(expected.rsplit("/", 1)[1]) - 1]
    assert note == ""
