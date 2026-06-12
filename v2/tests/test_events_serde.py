"""Every event kind round-trips through envelope_to_dict / envelope_from_dict."""

from __future__ import annotations

import json

import pytest

from forge.kernel.events import (
    DURABLE_KINDS,
    TRANSIENT_KINDS,
    AssistantBlock,
    ChildSpawned,
    Compacted,
    Envelope,
    PermissionAsked,
    PermissionDecided,
    RetryScheduled,
    SessionEnded,
    TextDelta,
    ThinkingDelta,
    ToolDeclared,
    ToolFinished,
    ToolOutputChunk,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UserSubmitted,
    envelope_from_dict,
    envelope_to_dict,
    is_durable,
    make_envelope,
)
from forge.kernel.types import (
    PermissionQuestion,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    Usage,
)

CALL = ToolCall("c1", "read", {"path": "src/a.py", "limit": 40, "nested": {"k": [1, 2]}})

DURABLE_SAMPLES = [
    UserSubmitted("fix the bug"),
    TurnStarted(3),
    AssistantBlock(TextBlock("hello")),
    AssistantBlock(ThinkingBlock("hmm")),
    AssistantBlock(CALL),
    ToolDeclared(CALL),
    ToolStarted("c1"),
    ToolFinished(ToolResult("c1", "file contents", is_error=False)),
    ToolFinished(ToolResult("c1", "boom", is_error=True)),
    PermissionAsked(PermissionQuestion("c1", "bash", "run `rm -rf build`?")),
    PermissionDecided("c1", allowed=True, source="user", reason=""),
    PermissionDecided("c2", allowed=False, source="policy", reason="outside workspace"),
    Compacted(summary="we did things", first_kept_seq=0),
    RetryScheduled(attempt=2, delay_s=1.5, reason="overloaded"),
    TurnFinished(outcome="ok", usage=Usage(100, 50, 7, 3), cost=0.0123),
    TurnFinished(outcome="aborted", usage=Usage(), cost=None),
    ChildSpawned("child-sid-1"),
    SessionEnded(),
]

TRANSIENT_SAMPLES = [
    TextDelta("par"),
    ThinkingDelta("tial"),
    ToolOutputChunk("c1", "chunk\n"),
]


def _round_trip(env: Envelope) -> Envelope:
    wire = json.dumps(envelope_to_dict(env))
    return envelope_from_dict(json.loads(wire))


@pytest.mark.parametrize("event", DURABLE_SAMPLES, ids=lambda e: type(e).__name__)
def test_durable_event_round_trips(event) -> None:
    env = make_envelope(seq=7, sid="s1", body=event, parent="p0", ts=123.5)
    assert env.durable is True
    assert _round_trip(env) == env


@pytest.mark.parametrize("event", TRANSIENT_SAMPLES, ids=lambda e: type(e).__name__)
def test_transient_event_round_trips(event) -> None:
    env = make_envelope(seq=8, sid="s1", body=event)
    assert env.durable is False
    assert is_durable(event) is False
    assert _round_trip(env) == env


def test_every_durable_kind_is_covered() -> None:
    sampled = {type(e).__name__ for e in DURABLE_SAMPLES}
    assert sampled == set(DURABLE_KINDS)
    assert {type(e).__name__ for e in TRANSIENT_SAMPLES} == set(TRANSIENT_KINDS)


def test_parent_none_survives() -> None:
    env = make_envelope(seq=0, sid="s1", body=SessionEnded())
    assert env.parent is None
    assert _round_trip(env).parent is None


def test_unknown_kind_rejected() -> None:
    d = envelope_to_dict(make_envelope(seq=0, sid="s1", body=UserSubmitted("x")))
    d["kind"] = "Bogus"
    with pytest.raises(ValueError, match="unknown event kind"):
        envelope_from_dict(d)


def test_future_version_rejected() -> None:
    d = envelope_to_dict(make_envelope(seq=0, sid="s1", body=UserSubmitted("x")))
    d["v"] = 99
    with pytest.raises(ValueError, match="newer than supported"):
        envelope_from_dict(d)


def test_older_version_needs_upconverter() -> None:
    d = envelope_to_dict(make_envelope(seq=0, sid="s1", body=UserSubmitted("x")))
    d["v"] = 0
    with pytest.raises(ValueError, match="no up-converter"):
        envelope_from_dict(d)
