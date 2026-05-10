"""Round-trip and contract tests for wiki/types.py."""
from __future__ import annotations

from datetime import datetime, timezone

from agent_forge.wiki.types import Artifact, GatherResult, Gatherer, Source


def test_artifact_construction_minimal_fields():
    a = Artifact(
        id="pr-1", kind="pr", source=Source.BUILTIN,
        title="t", body="b", ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert a.area is None
    assert a.signals == {}


def test_artifact_signals_default_is_per_instance():
    a = Artifact(id="x", kind="pr", source=Source.BUILTIN, title="", body="", ts=datetime.now(timezone.utc))
    b = Artifact(id="y", kind="pr", source=Source.BUILTIN, title="", body="", ts=datetime.now(timezone.utc))
    a.signals["k"] = "v"
    assert "k" not in b.signals  # defaults aren't shared


def test_source_enum_string_value():
    assert Source.BUILTIN == "builtin"
    assert Source.NOTE.value == "note"


def test_gatherer_default_attrs():
    g = Gatherer()
    assert g.name == ""
    assert g.runs_after == ()
    assert g.timeout_seconds == 60


def test_gatherer_subclass_can_override():
    class X(Gatherer):
        name = "x"
        runs_after = ("y",)
        timeout_seconds = 10
    assert X().name == "x"
    assert X.runs_after == ("y",)


def test_gather_result_construction():
    r = GatherResult(
        artifacts_added=3,
        by_kind={"pr": 2, "commit": 1},
        areas_touched=("payments",),
        cursor_advanced_to=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert r.errors == ()
    assert r.by_kind["pr"] == 2
