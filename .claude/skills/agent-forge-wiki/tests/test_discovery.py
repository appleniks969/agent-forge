"""Tests for discovery — topo sort, user-gatherer loading, error isolation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki import storage
from wiki.gather import discovery
from wiki.types import Artifact, Gatherer, Source


# ── Topo sort ─────────────────────────────────────────────────────────────────

def _make(name, runs_after=()):
    g = Gatherer()
    g.name = name
    g.runs_after = tuple(runs_after)
    return g


def test_topo_sort_orders_by_runs_after():
    a = _make("a")
    b = _make("b", runs_after=("a",))
    c = _make("c", runs_after=("b",))
    ordered = discovery._topo_sort([c, a, b])
    names = [g.name for g in ordered]
    assert names.index("a") < names.index("b") < names.index("c")


def test_topo_sort_ignores_unknown_dep():
    a = _make("a", runs_after=("nonexistent",))
    ordered = discovery._topo_sort([a])
    assert [g.name for g in ordered] == ["a"]


def test_topo_sort_with_cycle_falls_back_to_declaration_order():
    a = _make("a", runs_after=("b",))
    b = _make("b", runs_after=("a",))
    ordered = discovery._topo_sort([a, b])
    assert {g.name for g in ordered} == {"a", "b"}


# ── User gatherer loading ─────────────────────────────────────────────────────

def test_load_user_gatherers_imports_and_instantiates(tmp_path):
    d = storage.gatherers_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "myg.py").write_text(
        "from wiki.types import Gatherer\n"
        "class MyG(Gatherer):\n"
        "    name = 'myg'\n"
    )
    found = discovery._load_user_gatherers(tmp_path)
    assert any(g.name == "myg" for g in found)


def test_load_user_gatherers_skips_files_starting_with_underscore(tmp_path):
    d = storage.gatherers_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_skip.py").write_text(
        "from wiki.types import Gatherer\n"
        "class S(Gatherer):\n    name = 's'\n"
    )
    assert discovery._load_user_gatherers(tmp_path) == []


def test_load_user_gatherers_logs_import_failure(tmp_path):
    d = storage.gatherers_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.py").write_text("import nonexistent_pkg_xyz\n")
    found = discovery._load_user_gatherers(tmp_path)
    assert found == []
    log_text = storage.gather_log_path(tmp_path).read_text()
    assert "broken.py" in log_text


def test_load_user_gatherers_no_dir_returns_empty(tmp_path):
    assert discovery._load_user_gatherers(tmp_path) == []


# ── End-to-end run_gather with fake gatherers ─────────────────────────────────

class _FakeGatherer(Gatherer):
    name = "fake"

    async def gather(self, repo_root, since, cursor):
        return [Artifact(
            id="fake-1", kind="note", source=Source.BUILTIN,
            title="x", body="y", ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            signals={"path": "src/x.py"},
        )]


class _BoomGatherer(Gatherer):
    name = "boom"

    async def gather(self, repo_root, since, cursor):
        raise RuntimeError("kaboom")


class _SlowGatherer(Gatherer):
    name = "slow"
    timeout_seconds = 0  # forces immediate timeout

    async def gather(self, repo_root, since, cursor):
        await asyncio.sleep(1.0)
        return []


@pytest.mark.asyncio
async def test_run_gather_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeGatherer, _BoomGatherer))
    result = await discovery.run_gather(tmp_path)
    assert result.artifacts_added == 1
    assert any("boom" in e for e in result.errors)
    assert result.by_kind == {"note": 1}


@pytest.mark.asyncio
async def test_run_gather_dedupes_via_sha_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeGatherer,))
    r1 = await discovery.run_gather(tmp_path)
    r2 = await discovery.run_gather(tmp_path)
    assert r1.artifacts_added == 1
    assert r2.artifacts_added == 0   # SHA-cached


@pytest.mark.asyncio
async def test_run_gather_persists_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeGatherer,))
    await discovery.run_gather(tmp_path)
    cursor = storage.read_cursor(tmp_path)
    assert "last_run_ts" in cursor


@pytest.mark.asyncio
async def test_run_gather_only_runs_subset(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeGatherer, _BoomGatherer))
    result = await discovery.run_gather(tmp_path, only=["fake"])
    assert result.artifacts_added == 1
    assert result.errors == ()  # boom wasn't selected


@pytest.mark.asyncio
async def test_run_gather_marks_dirty_areas(tmp_path, monkeypatch):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  app:\n    paths:\n      - src/**\n"
    )
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeGatherer,))
    await discovery.run_gather(tmp_path)
    assert storage.read_dirty(tmp_path) == {"app"}


@pytest.mark.asyncio
async def test_run_gather_handles_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "BUILTINS", (_SlowGatherer,))
    result = await discovery.run_gather(tmp_path)
    assert any("timeout" in e for e in result.errors)
    assert result.artifacts_added == 0
