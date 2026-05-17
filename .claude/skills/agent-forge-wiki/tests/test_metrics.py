"""Tests for wiki/metrics.py — citation/override logging + summary."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from wiki import storage
from wiki.metrics import (
    MetricsSummary, record_citation, record_override,
    snapshot_staleness, summarise,
)
from wiki.types import Artifact, Source


def _commit(id_, area, ts):
    return Artifact(
        id=id_, kind="commit", source=Source.BUILTIN,
        title="t", body="b", ts=ts, area=area, signals={},
    )


# ── Recorders ─────────────────────────────────────────────────────────────────

def test_record_citation_writes_jsonl(tmp_path):
    record_citation(tmp_path, session_id="abc", turn=3,
                    source_id="curated/onboarding.md", snippet="see refund.py")
    p = tmp_path / ".agent-forge" / "metrics" / "citations.jsonl"
    assert p.exists()
    entry = json.loads(p.read_text(encoding="utf-8").strip())
    assert entry["session_id"] == "abc"
    assert entry["turn"] == 3
    assert entry["source_id"] == "curated/onboarding.md"


def test_record_override_writes_jsonl(tmp_path):
    record_override(tmp_path, session_id="abc", turn=5,
                    citation_id="curated/onboarding.md",
                    user_correction="that's wrong about retries")
    p = tmp_path / ".agent-forge" / "metrics" / "overrides.jsonl"
    assert p.exists()
    entry = json.loads(p.read_text(encoding="utf-8").strip())
    assert entry["correction"] == "that's wrong about retries"


def test_recorders_never_raise_on_io_error(tmp_path):
    # Point at a file path where the parent is a regular file (forces mkdir to fail).
    junk = tmp_path / "forge"
    junk.write_text("not a dir")
    # Should silently swallow. Smoke-test only — we can't easily assert the
    # internal path, so just check it doesn't raise.
    record_citation(tmp_path / "forge" / "x", session_id="s", turn=1, source_id="x")


# ── Staleness snapshot ────────────────────────────────────────────────────────

def test_snapshot_staleness_returns_neg_when_never_gathered(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  payments:\n    paths:\n      - src/payments/**\n"
    )
    storage.write_artifact(tmp_path, _commit(
        "c-1", "payments", datetime(2025, 6, 1, tzinfo=timezone.utc),
    ))
    out = snapshot_staleness(tmp_path)
    assert out == {"payments": -1}


def test_snapshot_staleness_with_gather_cursor(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  payments:\n    paths:\n      - src/payments/**\n"
    )
    storage.write_artifact(tmp_path, _commit(
        "c-1", "payments", datetime(2025, 6, 10, tzinfo=timezone.utc),
    ))
    storage.write_cursor(tmp_path, {"last_run_ts": "2025-06-01T00:00:00+00:00"})
    out = snapshot_staleness(tmp_path)
    # 9 days lag (commit 6/10, gather 6/01).
    assert out["payments"] == 9


def test_snapshot_staleness_writes_snapshot_file(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_cursor(tmp_path, {"last_run_ts": "2025-01-01T00:00:00+00:00"})
    snapshot_staleness(tmp_path)
    snap = tmp_path / ".agent-forge" / "metrics" / "staleness.json"
    assert snap.exists()
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert "snapshot_ts" in data
    assert "lag_days" in data


# ── Summary ───────────────────────────────────────────────────────────────────

def test_summarise_counts_in_window(tmp_path):
    record_citation(tmp_path, session_id="s", turn=1, source_id="curated/a.md")
    record_citation(tmp_path, session_id="s", turn=2, source_id="curated/a.md")
    record_citation(tmp_path, session_id="s", turn=3, source_id="curated/b.md")
    record_override(tmp_path, session_id="s", turn=4, citation_id="curated/a.md",
                    user_correction="nope")
    s = summarise(tmp_path)
    assert isinstance(s, MetricsSummary)
    assert s.citations_n == 3
    assert s.overrides_n == 1
    by_src = dict(s.citation_sources)
    assert by_src["curated/a.md"] == 2
    assert by_src["curated/b.md"] == 1


def test_summarise_empty(tmp_path):
    s = summarise(tmp_path)
    assert s.citations_n == 0
    assert s.overrides_n == 0
    assert s.citation_sources == ()


def test_summarise_skips_stale_entries(tmp_path):
    """Old entries beyond the window aren't counted."""
    p = tmp_path / ".agent-forge" / "metrics" / "citations.jsonl"
    p.parent.mkdir(parents=True)
    # One ancient entry (year 2000).
    p.write_text(
        json.dumps({"ts": "2000-01-01T00:00:00+00:00", "session_id": "s",
                    "turn": 1, "source_id": "x"}) + "\n",
        encoding="utf-8",
    )
    s = summarise(tmp_path, last_n_days=14)
    assert s.citations_n == 0
