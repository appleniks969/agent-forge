"""Secret-pattern redactor for rewrite-before-append on the session log.

Layer: adapters — imports kernel only. Applied by JsonlStore before a durable
envelope is serialized, so disk, fold, and resume never see raw keys.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from forge.kernel.events import (
    AssistantTurn,
    Compacted,
    Event,
    ToolFinished,
    UserSubmitted,
)
from forge.kernel.types import TextBlock, ThinkingBlock, ToolCall, ToolResult

_REDACTED = "[redacted]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-+=/]{8,}", re.IGNORECASE),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*"
        r"['\"]?([^\s'\"]{8,})"
    ),
)


def redact_text(text: str) -> str:
    out = text
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
        else:
            out = pat.sub(_REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def _redact_block(block: TextBlock | ThinkingBlock | ToolCall) -> TextBlock | ThinkingBlock | ToolCall:
    if isinstance(block, TextBlock):
        return TextBlock(redact_text(block.text))
    if isinstance(block, ThinkingBlock):
        return ThinkingBlock(redact_text(block.text))
    if isinstance(block, ToolCall):
        return ToolCall(block.id, block.name, _redact_value(dict(block.args)))
    return block


def secret_redactor(event: Event) -> Event:
    """Default JsonlStore redactor: mask common credential patterns."""
    match event:
        case ToolFinished(result):
            return ToolFinished(
                ToolResult(
                    result.call_id,
                    redact_text(result.content),
                    result.is_error,
                )
            )
        case UserSubmitted(text):
            return UserSubmitted(redact_text(text))
        case AssistantTurn(blocks, usage):
            return AssistantTurn(tuple(_redact_block(b) for b in blocks), usage)
        case Compacted(summary, first_kept_seq, usage):
            return Compacted(redact_text(summary), first_kept_seq, usage)
        case _:
            return event
