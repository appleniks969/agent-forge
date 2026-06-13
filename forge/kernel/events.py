"""The Event union, Envelope, and JSON serde.

Layer: kernel — imports kernel siblings only. Durable events are the only
source of truth (fsync'd by the store before anything else sees them);
transient events feed live rendering and never enter folded state. Each
event kind carries its own schema version `v`; up-conversion is a small
registry of dict->dict functions, not a framework.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from forge.kernel.types import (
    Block,
    DecisionSource,
    Outcome,
    PermissionQuestion,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    Usage,
)

# --- durable events ---------------------------------------------------------


@dataclass(frozen=True)
class UserSubmitted:
    text: str


@dataclass(frozen=True)
class TurnStarted:
    turn: int


@dataclass(frozen=True)
class AssistantTurn:
    """One model round: all of the round's blocks plus its usage. Carrying the
    whole round in one event (not block-by-block) lets fold be a plain reduce
    with no buffering, and makes usage event-driven so fold == live exactly."""

    blocks: tuple[Block, ...]
    usage: Usage


@dataclass(frozen=True)
class ToolDeclared:
    call: ToolCall


@dataclass(frozen=True)
class ToolStarted:
    call_id: str


@dataclass(frozen=True)
class ToolFinished:
    result: ToolResult


@dataclass(frozen=True)
class PermissionAsked:
    question: PermissionQuestion


@dataclass(frozen=True)
class PermissionDecided:
    call_id: str
    allowed: bool
    source: DecisionSource
    reason: str


@dataclass(frozen=True)
class Compacted:
    # first_kept_seq is 0 while compaction fully replaces the window: no
    # pre-compaction message survives; the live window is summary + later events.
    summary: str
    first_kept_seq: int
    usage: Usage  # the compaction call's usage — accumulated like a model round


@dataclass(frozen=True)
class RetryScheduled:
    attempt: int
    delay_s: float
    reason: str


@dataclass(frozen=True)
class TurnFinished:
    outcome: Outcome
    usage: Usage  # this turn's usage (model rounds + compaction), not session totals
    cost: float | None


@dataclass(frozen=True)
class ChildSpawned:
    child_sid: str


@dataclass(frozen=True)
class SessionEnded:
    pass


# --- transient events (bus-only; durable=False, never folded) ----------------


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolOutputChunk:
    call_id: str
    text: str


DurableEvent = (
    UserSubmitted
    | TurnStarted
    | AssistantTurn
    | ToolDeclared
    | ToolStarted
    | ToolFinished
    | PermissionAsked
    | PermissionDecided
    | Compacted
    | RetryScheduled
    | TurnFinished
    | ChildSpawned
    | SessionEnded
)
TransientEvent = TextDelta | ThinkingDelta | ToolOutputChunk
Event = DurableEvent | TransientEvent


@dataclass(frozen=True)
class Envelope:
    seq: int
    sid: str
    parent: str | None
    ts: float
    v: int
    durable: bool
    body: Event


# --- serde: nested value codecs ----------------------------------------------

Mapping_any = dict[str, Any]


def _enc_block(b: Block) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "text": b.text}
    return {"type": "tool_call", "id": b.id, "name": b.name, "args": dict(b.args)}


def _dec_block(d: Mapping_any) -> Block:
    match d["type"]:
        case "text":
            return TextBlock(d["text"])
        case "thinking":
            return ThinkingBlock(d["text"])
        case "tool_call":
            return ToolCall(id=d["id"], name=d["name"], args=d["args"])
        case other:
            raise ValueError(f"unknown block type: {other!r}")


def _enc_call(c: ToolCall) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "args": dict(c.args)}


def _dec_call(d: Mapping_any) -> ToolCall:
    return ToolCall(id=d["id"], name=d["name"], args=d["args"])


def _dec_result(d: Mapping_any) -> ToolResult:
    return ToolResult(call_id=d["call_id"], content=d["content"], is_error=d["is_error"])


def _dec_usage(d: Mapping_any) -> Usage:
    return Usage(**d)


# --- serde: per-event-kind registry ------------------------------------------


@dataclass(frozen=True)
class _Codec:
    kind: str
    v: int
    cls: type
    enc: Callable[[Any], dict[str, Any]]
    dec: Callable[[dict[str, Any]], Any]


def _flat(cls: type, v: int = 1) -> _Codec:
    def dec(d: dict[str, Any], _cls: type = cls) -> Any:
        return _cls(**d)

    return _Codec(cls.__name__, v, cls, asdict, dec)


_CODECS: tuple[_Codec, ...] = (
    _flat(UserSubmitted),
    _flat(TurnStarted),
    _Codec(
        "AssistantTurn",
        1,
        AssistantTurn,
        lambda e: {"blocks": [_enc_block(b) for b in e.blocks], "usage": asdict(e.usage)},
        lambda d: AssistantTurn(
            tuple(_dec_block(b) for b in d["blocks"]), _dec_usage(d["usage"])
        ),
    ),
    _Codec(
        "ToolDeclared",
        1,
        ToolDeclared,
        lambda e: {"call": _enc_call(e.call)},
        lambda d: ToolDeclared(_dec_call(d["call"])),
    ),
    _flat(ToolStarted),
    _Codec(
        "ToolFinished",
        1,
        ToolFinished,
        lambda e: {"result": asdict(e.result)},
        lambda d: ToolFinished(_dec_result(d["result"])),
    ),
    _Codec(
        "PermissionAsked",
        1,
        PermissionAsked,
        lambda e: {"question": asdict(e.question)},
        lambda d: PermissionAsked(PermissionQuestion(**d["question"])),
    ),
    _flat(PermissionDecided),
    _Codec(
        "Compacted",
        1,
        Compacted,
        lambda e: {"summary": e.summary, "first_kept_seq": e.first_kept_seq, "usage": asdict(e.usage)},
        lambda d: Compacted(
            summary=d["summary"], first_kept_seq=d["first_kept_seq"], usage=_dec_usage(d["usage"])
        ),
    ),
    _flat(RetryScheduled),
    _Codec(
        "TurnFinished",
        1,
        TurnFinished,
        lambda e: {"outcome": e.outcome, "usage": asdict(e.usage), "cost": e.cost},
        lambda d: TurnFinished(outcome=d["outcome"], usage=_dec_usage(d["usage"]), cost=d["cost"]),
    ),
    _flat(ChildSpawned),
    _flat(SessionEnded),
    _flat(TextDelta),
    _flat(ThinkingDelta),
    _flat(ToolOutputChunk),
)

_BY_KIND: dict[str, _Codec] = {c.kind: c for c in _CODECS}
_BY_TYPE: dict[type, _Codec] = {c.cls: c for c in _CODECS}

DURABLE_KINDS: frozenset[str] = frozenset(
    c.kind for c in _CODECS if not issubclass(c.cls, (TextDelta, ThinkingDelta, ToolOutputChunk))
)
TRANSIENT_KINDS: frozenset[str] = frozenset(_BY_KIND) - DURABLE_KINDS

# (kind, from_v) -> dict-level up-converter producing the from_v+1 shape
_UPCONVERTERS: dict[tuple[str, int], Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_upconverter(
    kind: str, from_v: int, fn: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    _UPCONVERTERS[(kind, from_v)] = fn


def kind_of(event: Event) -> str:
    return _BY_TYPE[type(event)].kind


def version_of(event: Event) -> int:
    return _BY_TYPE[type(event)].v


def is_durable(event: Event) -> bool:
    return kind_of(event) in DURABLE_KINDS


def make_envelope(
    seq: int,
    sid: str,
    body: Event,
    *,
    parent: str | None = None,
    ts: float = 0.0,
) -> Envelope:
    return Envelope(
        seq=seq,
        sid=sid,
        parent=parent,
        ts=ts,
        v=version_of(body),
        durable=is_durable(body),
        body=body,
    )


def envelope_to_dict(env: Envelope) -> dict[str, Any]:
    codec = _BY_TYPE.get(type(env.body))
    if codec is None:
        raise ValueError(f"unregistered event type: {type(env.body).__name__}")
    return {
        "seq": env.seq,
        "sid": env.sid,
        "parent": env.parent,
        "ts": env.ts,
        "v": env.v,
        "durable": env.durable,
        "kind": codec.kind,
        "body": codec.enc(env.body),
    }


def envelope_from_dict(d: dict[str, Any]) -> Envelope:
    kind = d["kind"]
    codec = _BY_KIND.get(kind)
    if codec is None:
        raise ValueError(f"unknown event kind: {kind!r}")
    v = d["v"]
    if v > codec.v:
        raise ValueError(f"{kind} v{v} is newer than supported v{codec.v}")
    body_dict = d["body"]
    while v < codec.v:
        up = _UPCONVERTERS.get((kind, v))
        if up is None:
            raise ValueError(f"no up-converter for {kind} v{v} -> v{v + 1}")
        body_dict = up(body_dict)
        v += 1
    return Envelope(
        seq=d["seq"],
        sid=d["sid"],
        parent=d.get("parent"),
        ts=d["ts"],
        v=v,
        durable=d["durable"],
        body=codec.dec(body_dict),
    )
