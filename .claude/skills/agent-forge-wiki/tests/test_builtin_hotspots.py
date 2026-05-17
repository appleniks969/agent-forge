"""Tests for hotspots gatherer — second-pass over already-written raw/."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wiki import storage
from wiki.gather.builtin.hotspots import HotspotsGatherer
from wiki.types import Artifact, Source


def _commit(id_, ts, files, *, is_bugfix=False, author=None):
    sig = {"files_changed": files}
    if is_bugfix:
        sig["is_bugfix"] = True
    if author is not None:
        sig["author"] = author
    return Artifact(
        id=f"commit-{id_}", kind="commit", source=Source.BUILTIN,
        title="t", body="", ts=ts, signals=sig,
    )


def _pr(number, ts, files, *, is_bugfix=False):
    return Artifact(
        id=f"pr-{number}", kind="pr", source=Source.BUILTIN,
        title="t", body="", ts=ts,
        signals={"files_changed": files, **({"is_bugfix": True} if is_bugfix else {})},
    )


@pytest.mark.asyncio
async def test_hotspots_emits_per_file_signals_from_commits(tmp_path):
    storage.ensure_layout(tmp_path)
    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=10)
    older  = now - timedelta(days=60)

    storage.write_artifact(tmp_path, _commit("a1", recent, ["src/payments/refund.py"], is_bugfix=True))
    storage.write_artifact(tmp_path, _commit("a2", recent, ["src/payments/refund.py"], is_bugfix=True))
    storage.write_artifact(tmp_path, _commit("a3", older,  ["src/payments/refund.py"]))

    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    file_hot = [a for a in out if a.signals.get("path") == "src/payments/refund.py"]
    assert file_hot
    h = file_hot[0]
    assert h.kind == "hotspot"
    assert h.source == Source.DERIVED
    assert h.signals["bugfix_count_30d"] == 2
    assert h.signals["bugfix_count_90d"] == 2
    assert h.signals["total_changes_90d"] == 3


@pytest.mark.asyncio
async def test_hotspots_does_area_attribution_via_contexts(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  payments:\n    paths:\n      - src/payments/**\n"
    )
    now = datetime.now(timezone.utc)
    storage.write_artifact(
        tmp_path,
        _commit("x", now - timedelta(days=5), ["src/payments/refund.py"], is_bugfix=True),
    )
    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    file_hot = next(a for a in out if a.signals.get("path") == "src/payments/refund.py")
    assert file_hot.area == "payments"

    # Area-level rollup is also emitted.
    rollups = [a for a in out if a.signals.get("scope") == "area"]
    assert any(r.area == "payments" for r in rollups)


@pytest.mark.asyncio
async def test_hotspots_picks_up_codeowners(tmp_path):
    storage.ensure_layout(tmp_path)
    # Inject a repo_file artifact carrying parsed codeowners.
    co_artifact = Artifact(
        id="repo_file-codeowners", kind="repo_file", source=Source.BUILTIN,
        title="CODEOWNERS", body="src/payments/** @sara",
        ts=datetime.now(timezone.utc),
        signals={"path": "CODEOWNERS",
                 "codeowners": {"src/payments/**": ["@sara"]}},
    )
    storage.write_artifact(tmp_path, co_artifact)

    now = datetime.now(timezone.utc)
    storage.write_artifact(
        tmp_path,
        _commit("y", now - timedelta(days=2), ["src/payments/refund.py"], is_bugfix=True),
    )
    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    file_hot = next(a for a in out if a.signals.get("path") == "src/payments/refund.py")
    assert "@sara" in file_hot.signals["owners"]


@pytest.mark.asyncio
async def test_hotspots_returns_empty_when_no_data(tmp_path):
    storage.ensure_layout(tmp_path)
    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    assert out == []


# ── Owner fallback (authorship-based, when CODEOWNERS doesn't claim a path) ──

@pytest.mark.asyncio
async def test_hotspots_owner_fallback_uses_top_authors_when_no_codeowners(tmp_path):
    """No CODEOWNERS → owners come from top authors over 90 days."""
    storage.ensure_layout(tmp_path)
    now = datetime.now(timezone.utc)
    # Sara: 3 commits to the file. Marcus: 1. Alice: 0 (different file).
    for i in range(3):
        storage.write_artifact(tmp_path, _commit(
            f"s{i}", now - timedelta(days=i + 1),
            ["src/payments/refund.py"], is_bugfix=True, author="Sara",
        ))
    storage.write_artifact(tmp_path, _commit(
        "m1", now - timedelta(days=5),
        ["src/payments/refund.py"], is_bugfix=True, author="Marcus",
    ))
    storage.write_artifact(tmp_path, _commit(
        "a1", now - timedelta(days=2),
        ["src/auth/oauth.py"], is_bugfix=True, author="Alice",
    ))

    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    refund = next(a for a in out if a.signals.get("path") == "src/payments/refund.py")
    # Sara first (most commits), then Marcus. Alice never touched this file.
    assert refund.signals["owners"][:2] == ["Sara", "Marcus"]
    assert refund.signals["owners_source"] == "authors_90d"


@pytest.mark.asyncio
async def test_hotspots_owner_fallback_filters_out_bots(tmp_path):
    """Bot authors must NEVER appear in fallback owners — they pollute the
    signal on every repo with renovate / dependabot / github-actions."""
    storage.ensure_layout(tmp_path)
    now = datetime.now(timezone.utc)
    # Bot dominates by volume; one human contributor.
    for i in range(20):
        storage.write_artifact(tmp_path, _commit(
            f"b{i}", now - timedelta(days=i),
            ["package.json"], is_bugfix=True, author="dependabot[bot]",
        ))
    storage.write_artifact(tmp_path, _commit(
        "h1", now - timedelta(days=3),
        ["package.json"], is_bugfix=True, author="Sara",
    ))

    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    pkg = next((a for a in out if a.signals.get("path") == "package.json"), None)
    assert pkg is not None
    # Sara surfaces; bot does not.
    assert "Sara" in pkg.signals["owners"]
    assert all("bot" not in o.lower() for o in pkg.signals["owners"])
    assert pkg.signals["owners_source"] == "authors_90d"


@pytest.mark.asyncio
async def test_hotspots_codeowners_takes_precedence_over_authors(tmp_path):
    """When CODEOWNERS claims a path, authorship fallback is *not* used."""
    storage.ensure_layout(tmp_path)
    co_artifact = Artifact(
        id="repo_file-codeowners", kind="repo_file", source=Source.BUILTIN,
        title="CODEOWNERS", body="src/payments/** @declared-team",
        ts=datetime.now(timezone.utc),
        signals={"path": "CODEOWNERS",
                 "codeowners": {"src/payments/**": ["@declared-team"]}},
    )
    storage.write_artifact(tmp_path, co_artifact)

    now = datetime.now(timezone.utc)
    storage.write_artifact(tmp_path, _commit(
        "x", now - timedelta(days=2),
        ["src/payments/refund.py"], is_bugfix=True, author="Sara",
    ))

    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    refund = next(a for a in out if a.signals.get("path") == "src/payments/refund.py")
    assert refund.signals["owners"] == ["@declared-team"]
    assert refund.signals["owners_source"] == "codeowners"


@pytest.mark.asyncio
async def test_hotspots_owners_source_none_when_no_signal(tmp_path):
    """No CODEOWNERS, no authorship (e.g. all commits authored by bots) →
    owners_source='none' so renderers can suppress the field cleanly."""
    storage.ensure_layout(tmp_path)
    now = datetime.now(timezone.utc)
    # Only bot commits — fallback yields []; no CODEOWNERS exists.
    for i in range(5):
        storage.write_artifact(tmp_path, _commit(
            f"b{i}", now - timedelta(days=i),
            ["src/x.py"], is_bugfix=True, author="renovate[bot]",
        ))

    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    h = next(a for a in out if a.signals.get("path") == "src/x.py")
    assert h.signals["owners"] == []
    assert h.signals["owners_source"] == "none"


@pytest.mark.asyncio
async def test_hotspots_co_changed_files_populated(tmp_path):
    storage.ensure_layout(tmp_path)
    now = datetime.now(timezone.utc)
    # 3 commits all touching both files together → strong co-change signal.
    for i in range(3):
        storage.write_artifact(tmp_path, _commit(
            f"c{i}", now - timedelta(days=i),
            ["src/payments/refund.py", "src/payments/webhooks.py"],
            is_bugfix=True,
        ))
    g = HotspotsGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    refund = next(a for a in out if a.signals.get("path") == "src/payments/refund.py")
    assert "src/payments/webhooks.py" in refund.signals["co_changed_files"]
