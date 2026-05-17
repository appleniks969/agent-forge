"""Storage layer tests — paths, atomic writes, cursor, SHA cache, contexts.yaml."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from wiki import storage
from wiki.types import Artifact, Source


def _make_artifact(id_="pr-1", kind="pr", source=Source.BUILTIN, area=None, signals=None):
    return Artifact(
        id=id_, kind=kind, source=source,
        title="title", body="body",
        ts=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        area=area, signals=signals or {},
    )


# ── Paths ─────────────────────────────────────────────────────────────────────

def test_path_helpers(tmp_path):
    assert storage.wiki_root(tmp_path) == tmp_path / ".agent-forge"
    # raw_dir is a back-compat alias that now points at raw/cache/.
    assert storage.raw_dir(tmp_path) == tmp_path / ".agent-forge" / "raw" / "cache"
    assert storage.raw_root(tmp_path) == tmp_path / ".agent-forge" / "raw"
    assert storage.raw_cache_dir(tmp_path) == tmp_path / ".agent-forge" / "raw" / "cache"
    assert storage.raw_notes_dir(tmp_path) == tmp_path / ".agent-forge" / "raw" / "notes"
    assert storage.notes_dir(tmp_path) == tmp_path / ".agent-forge" / "notes"
    assert storage.curated_dir(tmp_path) == tmp_path / ".agent-forge" / "curated"
    assert storage.skills_dir(tmp_path) == tmp_path / ".agent-forge" / "skills"
    assert storage.metrics_dir(tmp_path) == tmp_path / ".agent-forge" / "metrics"
    assert storage.contexts_path(tmp_path).name == "contexts.yaml"


def test_ensure_layout_creates_dirs(tmp_path):
    storage.ensure_layout(tmp_path)
    assert (tmp_path / ".agent-forge" / "raw" / "cache" / ".cache").is_dir()
    assert (tmp_path / ".agent-forge" / "raw" / "notes").is_dir()


def test_write_session_insight(tmp_path):
    storage.ensure_layout(tmp_path)
    p = storage.write_session_insight(tmp_path, "abc123", "# session\nlearned X\n")
    assert p == tmp_path / ".agent-forge" / "raw" / "notes" / "session" / "abc123.md"
    assert p.read_text(encoding="utf-8").startswith("# session")


# ── Artifact write/read round-trip ────────────────────────────────────────────

def test_write_and_read_artifact(tmp_path):
    storage.ensure_layout(tmp_path)
    art = _make_artifact(signals={"is_bugfix": True, "files_changed": ["a.py", "b.py"]})
    path = storage.write_artifact(tmp_path, art)
    assert path.exists()
    assert path.parent.name == "prs"

    loaded = storage.read_artifact(path)
    assert loaded.id == art.id
    assert loaded.kind == art.kind
    assert loaded.source == Source.BUILTIN
    assert loaded.signals["is_bugfix"] is True
    assert loaded.ts == art.ts


def test_artifact_path_for_custom_source_lands_under_custom(tmp_path):
    art = _make_artifact(id_="x-1", kind="jira_bugs", source=Source.CUSTOM)
    path = storage.artifact_path(tmp_path, art)
    assert "custom" in path.parts
    assert "jira_bugs" in path.parts


def test_atomic_write_does_not_leave_tmp(tmp_path):
    storage.ensure_layout(tmp_path)
    art = _make_artifact()
    path = storage.write_artifact(tmp_path, art)
    leftover = list(path.parent.glob("*.tmp"))
    assert leftover == []


def test_list_artifacts_filtered_by_kind(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _make_artifact(id_="pr-1", kind="pr"))
    storage.write_artifact(tmp_path, _make_artifact(id_="c-1", kind="commit"))
    prs = list(storage.list_artifacts(tmp_path, kind="pr"))
    commits = list(storage.list_artifacts(tmp_path, kind="commit"))
    assert len(prs) == 1 and prs[0].kind == "pr"
    assert len(commits) == 1 and commits[0].kind == "commit"


def test_list_artifacts_walks_all_when_no_kind(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _make_artifact(id_="pr-1", kind="pr"))
    storage.write_artifact(tmp_path, _make_artifact(id_="adr-1", kind="adr"))
    all_arts = list(storage.list_artifacts(tmp_path))
    assert {a.kind for a in all_arts} == {"pr", "adr"}


def test_list_artifacts_tolerates_corrupt_files(tmp_path):
    storage.ensure_layout(tmp_path)
    bad = storage.raw_dir(tmp_path) / "prs" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not-json{")
    storage.write_artifact(tmp_path, _make_artifact(id_="pr-2", kind="pr"))
    arts = list(storage.list_artifacts(tmp_path, kind="pr"))
    assert len(arts) == 1


def test_list_artifacts_skips_cache_dir(tmp_path):
    storage.ensure_layout(tmp_path)
    # SHA cache lives in raw/.cache/sha.txt — never returned even on rglob.
    storage.sha_record(tmp_path, "abc")
    arts = list(storage.list_artifacts(tmp_path))
    assert arts == []


# ── Cursor ────────────────────────────────────────────────────────────────────

def test_cursor_round_trip(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_cursor(tmp_path, {"last_run_ts": "2025-01-01T00:00:00", "prs": {"last_number": 42}})
    c = storage.read_cursor(tmp_path)
    assert c["prs"]["last_number"] == 42


def test_cursor_missing_returns_empty_dict(tmp_path):
    assert storage.read_cursor(tmp_path) == {}


def test_cursor_corrupt_file_returns_empty_dict(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.cursor_path(tmp_path).write_text("garbage{not json")
    assert storage.read_cursor(tmp_path) == {}


# ── SHA cache ─────────────────────────────────────────────────────────────────

def test_sha_seen_and_record(tmp_path):
    storage.ensure_layout(tmp_path)
    assert not storage.sha_seen(tmp_path, "abc")
    storage.sha_record(tmp_path, "abc")
    assert storage.sha_seen(tmp_path, "abc")


# ── Dirty markers ─────────────────────────────────────────────────────────────

def test_dirty_round_trip(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.mark_dirty(tmp_path, ["payments", "auth"])
    assert storage.read_dirty(tmp_path) == {"payments", "auth"}
    storage.clear_dirty(tmp_path, "payments")
    assert storage.read_dirty(tmp_path) == {"auth"}


# ── contexts.yaml parser ──────────────────────────────────────────────────────

def test_load_contexts_missing_returns_empty(tmp_path):
    areas, inline = storage.load_contexts(tmp_path)
    assert areas == {}
    assert inline == set()


def test_load_contexts_parses_areas_and_inline_authors(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        """
# top-level comment
areas:
  payments:
    paths:
      - "src/payments/**"
      - "src/billing/**"
  auth:
    paths: ["src/auth/**"]

inline_comment_authors:
  - sara
  - "marcus"
  - alex
"""
    )
    areas, inline = storage.load_contexts(tmp_path)
    assert areas == {
        "payments": ["src/payments/**", "src/billing/**"],
        "auth": ["src/auth/**"],
    }
    assert inline == {"sara", "marcus", "alex"}


def test_load_contexts_handles_only_areas(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  payments:\n    paths:\n      - src/p/**\n"
    )
    areas, inline = storage.load_contexts(tmp_path)
    assert areas == {"payments": ["src/p/**"]}
    assert inline == set()


def test_load_contexts_handles_only_inline_authors(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "inline_comment_authors:\n  - sara\n  - marcus\n"
    )
    areas, inline = storage.load_contexts(tmp_path)
    assert areas == {}
    assert inline == {"sara", "marcus"}


# ── Area resolution ──────────────────────────────────────────────────────────

def test_areas_for_paths_matches_glob():
    areas = {
        "payments": ["src/payments/**"],
        "auth": ["src/auth/**"],
    }
    assert storage.areas_for_paths(["src/payments/refund.py"], areas) == {"payments"}
    assert storage.areas_for_paths(["src/auth/session.py"], areas) == {"auth"}
    assert storage.areas_for_paths(["docs/README.md"], areas) == set()


def test_areas_for_paths_multi_match():
    areas = {"a": ["src/**"], "b": ["src/foo/**"]}
    matched = storage.areas_for_paths(["src/foo/x.py"], areas)
    assert matched == {"a", "b"}
