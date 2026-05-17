"""Tests for the notes gatherer + front-matter parsing."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki import storage
from wiki.gather.builtin.notes import NotesGatherer, _parse_front_matter


def test_parse_front_matter_extracts_keys():
    text = """---
area: payments
tags: [decision, redis]
---
# body starts here
body line
"""
    fm, body = _parse_front_matter(text)
    assert fm == {"area": "payments", "tags": ["decision", "redis"]}
    assert body.startswith("# body starts here")


def test_parse_front_matter_no_fm_passes_through():
    text = "no front matter\nhere"
    fm, body = _parse_front_matter(text)
    assert fm == {}
    assert body == text


@pytest.mark.asyncio
async def test_notes_gatherer_emits_artifact_per_file(tmp_path):
    storage.ensure_layout(tmp_path)
    notes = storage.notes_dir(tmp_path)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "redis-decision.md").write_text(
        "---\narea: payments\ntags: [decision]\n---\nWhy redis.\n"
    )
    (notes / "no-fm.md").write_text("just plain content")

    g = NotesGatherer()
    arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    by_id = {a.id: a for a in arts}
    assert "note-redis-decision" in by_id
    assert "note-no-fm" in by_id
    a = by_id["note-redis-decision"]
    assert a.kind == "note"
    assert a.area == "payments"
    assert a.signals["tags"] == ["decision"]
    # Body is mirrored under raw/notes/manual/ as raw markdown (the sacred copy).
    raw_md = storage.raw_notes_dir(tmp_path) / "manual" / "redis-decision.md"
    assert raw_md.exists()


@pytest.mark.asyncio
async def test_notes_gatherer_no_dir_returns_empty(tmp_path):
    g = NotesGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    assert out == []
