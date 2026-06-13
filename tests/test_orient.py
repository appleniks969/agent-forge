"""Tests for the orientation suppliers (forge.front.orient).

Every test uses tmp_path — never the real repo. The git-path tests init a real
tmp git repo via subprocess and skip (not fail) if git is unavailable; the
non-git fallback is always exercised. The skills test stubs
forge.adapters.skills so it does not depend on Builder C's module being present.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from forge.front.orient import (
    agents_doc_supplier,
    memory_supplier,
    repo_map_supplier,
    skills_index_supplier,
)

_GIT = shutil.which("git")


def _bump_mtime(path: Path, *, delta: float = 10.0) -> None:
    """Push a file's mtime forward so a 1s-resolution filesystem still registers
    the change without the test having to sleep."""
    st = path.stat()
    import os

    os.utime(path, (st.st_atime + delta, st.st_mtime + delta))


# --- agents_doc_supplier --------------------------------------------------------


def test_agents_doc_hit_renders_header(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Build with uv.\n", encoding="utf-8")
    supply = agents_doc_supplier(tmp_path)
    text = supply()
    assert text is not None
    assert text.startswith("# Project instructions\n")
    assert "Build with uv." in text


def test_agents_doc_miss_returns_none(tmp_path: Path) -> None:
    assert agents_doc_supplier(tmp_path)() is None


def test_agents_doc_precedence_agents_over_claude(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("from agents", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("from claude", encoding="utf-8")
    text = agents_doc_supplier(tmp_path)()
    assert text is not None and "from agents" in text and "from claude" not in text


def test_agents_doc_falls_back_to_instructions(tmp_path: Path) -> None:
    inst = tmp_path / ".agent-forge" / "instructions.md"
    inst.parent.mkdir(parents=True)
    inst.write_text("instructed", encoding="utf-8")
    text = agents_doc_supplier(tmp_path)()
    assert text is not None and "instructed" in text


def test_agents_doc_cap_and_truncation_note(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * (40 * 1024), encoding="utf-8")
    text = agents_doc_supplier(tmp_path)()
    assert text is not None
    assert "[truncated — file exceeds 32KB]" in text
    # header + 32KB body + note, but never the full 40KB
    assert len(text) < 34 * 1024


def test_agents_doc_refreshes_on_mtime_change(tmp_path: Path) -> None:
    doc = tmp_path / "AGENTS.md"
    doc.write_text("v1", encoding="utf-8")
    supply = agents_doc_supplier(tmp_path)
    assert "v1" in (supply() or "")
    doc.write_text("v2", encoding="utf-8")
    _bump_mtime(doc)
    assert "v2" in (supply() or "")


def test_agents_doc_is_memoized_when_unchanged(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("stable", encoding="utf-8")
    supply = agents_doc_supplier(tmp_path)
    first = supply()
    second = supply()
    assert first is second  # cache hit returns the same object


# --- memory_supplier ------------------------------------------------------------


def _project_memory(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".agent-forge" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_memory_hit_renders_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect the global memory path to an empty dir so the host's real
    # ~/.agent-forge/memory.md never leaks into the test.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _project_memory(tmp_path, "- prefers tabs (learned 2026-01-01)")
    text = memory_supplier(tmp_path)()
    assert text is not None
    assert text.startswith("# Memory\n")
    assert "prefers tabs" in text


def test_memory_miss_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert memory_supplier(tmp_path)() is None


def test_memory_empty_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _project_memory(tmp_path, "   \n\n  ")
    assert memory_supplier(tmp_path)() is None


def test_memory_merges_global_and_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gp = home / ".agent-forge" / "memory.md"
    gp.parent.mkdir(parents=True)
    gp.write_text("- global fact", encoding="utf-8")
    _project_memory(tmp_path, "- project fact")
    text = memory_supplier(tmp_path)()
    assert text is not None
    assert "global fact" in text and "project fact" in text


def test_memory_refreshes_on_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    p = _project_memory(tmp_path, "- old fact")
    supply = memory_supplier(tmp_path)
    assert "old fact" in (supply() or "")
    p.write_text("- new fact", encoding="utf-8")
    _bump_mtime(p)
    assert "new fact" in (supply() or "")


def test_memory_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _project_memory(tmp_path, "- " + "y" * (10 * 1024))
    text = memory_supplier(tmp_path)()
    assert text is not None
    assert "[truncated — memory exceeds 8KB]" in text


# --- skills_index_supplier ------------------------------------------------------


@dataclass(frozen=True)
class _FakeMeta:
    name: str
    description: str
    path: Path


@pytest.fixture
def fake_skills(monkeypatch: pytest.MonkeyPatch):
    """Install a stub forge.adapters.skills with a settable skill list, so this
    test does not depend on the real adapter (Builder C) being present yet."""
    state: dict[str, tuple[_FakeMeta, ...]] = {"skills": ()}

    mod = types.ModuleType("forge.adapters.skills")

    def discover_skills(roots):  # signature-compatible with the contract
        return state["skills"]

    mod.discover_skills = discover_skills  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "forge.adapters.skills", mod)
    return state


def test_skills_index_hit_lists_skills(fake_skills, tmp_path: Path) -> None:
    fake_skills["skills"] = (
        _FakeMeta("deep-research", "fan-out web research", tmp_path / "a.md"),
        _FakeMeta("plan", "plan a change", tmp_path / "b.md"),
    )
    text = skills_index_supplier([tmp_path])()
    assert text is not None
    assert text.startswith("Available skills (invoke with the Skill tool):\n")
    assert "deep-research — fan-out web research" in text
    assert "plan — plan a change" in text


def test_skills_index_empty_returns_none(fake_skills, tmp_path: Path) -> None:
    fake_skills["skills"] = ()
    assert skills_index_supplier([tmp_path])() is None


def test_skills_index_missing_adapter_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the deferred import to fail; supplier must degrade to no section.
    monkeypatch.setitem(sys.modules, "forge.adapters.skills", None)
    assert skills_index_supplier([tmp_path])() is None


def test_skills_index_budget_overflow_note(fake_skills, tmp_path: Path) -> None:
    many = tuple(
        _FakeMeta(f"skill-{i:03d}", "d" * 200, tmp_path / f"{i}.md")
        for i in range(200)
    )
    fake_skills["skills"] = many
    text = skills_index_supplier([tmp_path])()
    assert text is not None
    assert "more skills" in text
    assert len(text.encode("utf-8")) <= 4 * 1024


# --- repo_map_supplier: non-git fallback (always runs) --------------------------


def test_repo_map_non_git_fallback_lists_files(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "README.md").write_text("r", encoding="utf-8")
    text = repo_map_supplier(tmp_path)()
    assert text is not None
    assert text.startswith("# Repository map\n")
    assert "pkg/a.py" in text
    assert "README.md" in text


def test_repo_map_non_git_ignores_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / "src.py").write_text("y", encoding="utf-8")
    text = repo_map_supplier(tmp_path)()
    assert text is not None
    assert "src.py" in text
    assert "junk.js" not in text


def test_repo_map_empty_dir_returns_none(tmp_path: Path) -> None:
    assert repo_map_supplier(tmp_path)() is None


def test_repo_map_byte_budget_with_overflow(tmp_path: Path) -> None:
    # Many small files in a non-git tree -> map must be clipped under budget with
    # an overflow line.
    for i in range(2000):
        (tmp_path / f"file_{i:04d}.txt").write_text("z", encoding="utf-8")
    text = repo_map_supplier(tmp_path)()
    assert text is not None
    assert "more files" in text
    assert len(text.encode("utf-8")) <= 6 * 1024


def test_repo_map_non_git_caches_on_dir_mtime(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("1", encoding="utf-8")
    supply = repo_map_supplier(tmp_path)
    first = supply()
    second = supply()
    assert first is second  # cache hit: identical object, no rescan


# --- repo_map_supplier: git path (skipped if git absent) ------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")


@pytest.mark.skipif(_GIT is None, reason="git not installed")
def test_repo_map_git_path_orders_recent_first(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "old.py").write_text("old", encoding="utf-8")
    _git(tmp_path, "add", "old.py")
    _git(tmp_path, "commit", "-q", "-m", "old")
    (tmp_path / "recent.py").write_text("recent", encoding="utf-8")
    _git(tmp_path, "add", "recent.py")
    _git(tmp_path, "commit", "-q", "-m", "recent")

    text = repo_map_supplier(tmp_path)()
    assert text is not None
    assert "recent.py" in text and "old.py" in text
    # Most-recently-committed file should appear before the older one in the
    # recency-weighted body.
    body = text.split("\n", 1)[1]  # drop header
    assert body.index("recent.py") < body.index("old.py")


@pytest.mark.skipif(_GIT is None, reason="git not installed")
def test_repo_map_git_excludes_untracked(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("t", encoding="utf-8")
    _git(tmp_path, "add", "tracked.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "untracked.py").write_text("u", encoding="utf-8")  # never added
    text = repo_map_supplier(tmp_path)()
    assert text is not None
    assert "tracked.py" in text
    assert "untracked.py" not in text


@pytest.mark.skipif(_GIT is None, reason="git not installed")
def test_repo_map_git_recomputes_on_new_commit(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "first.py").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "first.py")
    _git(tmp_path, "commit", "-q", "-m", "first")
    supply = repo_map_supplier(tmp_path)
    first_map = supply()
    assert first_map is not None and "second.py" not in first_map

    (tmp_path / "second.py").write_text("2", encoding="utf-8")
    _git(tmp_path, "add", "second.py")  # moves .git/index
    _git(tmp_path, "commit", "-q", "-m", "second")  # moves .git/HEAD
    git_dir = tmp_path / ".git"
    _bump_mtime(git_dir / "HEAD")
    _bump_mtime(git_dir / "index")
    second_map = supply()
    assert second_map is not None and "second.py" in second_map


@pytest.mark.skipif(_GIT is None, reason="git not installed")
def test_repo_map_git_cached_between_builds(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "f.py").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "f.py")
    _git(tmp_path, "commit", "-q", "-m", "c")
    supply = repo_map_supplier(tmp_path)
    first = supply()
    second = supply()
    assert first is second  # no git re-walk between builds when HEAD/index stable
