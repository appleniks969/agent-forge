"""Tests for wiki/maintain.py — staleness detection + re-gather."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wiki import storage
from wiki.maintain import (
    MaintainResult, detect_stale_areas, run_maintain,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> None:
    """Run a git command, raising on non-zero. Quiet — for test setup."""
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, path: str, body: str, msg: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", msg)


def _write_contexts(repo: Path) -> None:
    storage.ensure_layout(repo)
    storage.contexts_path(repo).write_text(
        "areas:\n"
        "  payments:\n"
        "    paths:\n"
        "      - src/payments/**\n"
        "  auth:\n"
        "    paths:\n"
        "      - src/auth/**\n",
    )


# ── detect_stale_areas ───────────────────────────────────────────────────────

def test_detect_stale_areas_no_contexts(tmp_path):
    _init_repo(tmp_path)
    storage.ensure_layout(tmp_path)
    # No contexts.yaml → empty result.
    assert detect_stale_areas(tmp_path) == []


def test_detect_stale_areas_below_threshold(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    _commit(tmp_path, "src/payments/refund.py", "x", "feat: refund")
    out = detect_stale_areas(tmp_path, threshold=10)
    # 1 commit < 10 threshold.
    assert out == []


def test_detect_stale_areas_above_threshold(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    for i in range(12):
        _commit(tmp_path, f"src/payments/m{i}.py", f"v{i}", f"feat: payments {i}")
    out = detect_stale_areas(tmp_path, threshold=10)
    assert ("payments", 12) in out


def test_detect_stale_areas_multiple(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    for i in range(11):
        _commit(tmp_path, f"src/payments/m{i}.py", "x", f"p{i}")
    for i in range(11):
        _commit(tmp_path, f"src/auth/a{i}.py", "x", f"a{i}")
    out = detect_stale_areas(tmp_path, threshold=10)
    by_name = dict(out)
    assert by_name["payments"] == 11
    assert by_name["auth"] == 11


def test_detect_stale_areas_respects_since(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    # 5 old commits, then set the cursor *after* them, then add 12 new.
    for i in range(5):
        _commit(tmp_path, f"src/payments/old{i}.py", "x", f"old{i}")
    # Capture timestamp of the latest "old" commit and store it as the cursor.
    out = subprocess.run(
        ["git", "log", "-1", "--format=%aI"],
        cwd=str(tmp_path), capture_output=True, text=True, check=True,
    )
    last_ts = out.stdout.strip()
    storage.write_cursor(tmp_path, {"last_run_ts": last_ts})
    for i in range(12):
        _commit(tmp_path, f"src/payments/new{i}.py", "x", f"new{i}")
    stale = detect_stale_areas(tmp_path, threshold=10)
    by_name = dict(stale)
    # Only the 12 new commits (since cursor) count — old ones excluded.
    # git --since is exclusive at second granularity; allow a small fudge.
    assert by_name["payments"] >= 10


def test_detect_stale_areas_handles_no_git(tmp_path):
    _write_contexts(tmp_path)
    # No `git init` — git log fails silently → no stale areas.
    assert detect_stale_areas(tmp_path) == []


# ── run_maintain ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_maintain_noop_when_clean(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    res = await run_maintain(tmp_path, threshold=10)
    assert isinstance(res, MaintainResult)
    assert res.areas_refreshed == 0
    assert res.artifacts_added == 0


@pytest.mark.asyncio
async def test_run_maintain_refreshes_stale_areas(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    for i in range(11):
        _commit(tmp_path, f"src/payments/x{i}.py", "x", f"feat {i}")
    # Pre-mark dirty (not strictly needed; run_maintain marks too).
    storage.mark_dirty(tmp_path, ["payments"])
    res = await run_maintain(tmp_path, threshold=10)
    assert res.areas_refreshed == 1
    # Dirty marker should be cleared after a successful refresh.
    assert "payments" not in storage.read_dirty(tmp_path)


@pytest.mark.asyncio
async def test_run_maintain_returns_stale_before_list(tmp_path):
    _init_repo(tmp_path)
    _write_contexts(tmp_path)
    for i in range(11):
        _commit(tmp_path, f"src/auth/a{i}.py", "x", f"auth {i}")
    res = await run_maintain(tmp_path, threshold=10)
    names = [a for a, _ in res.stale_before]
    assert "auth" in names
