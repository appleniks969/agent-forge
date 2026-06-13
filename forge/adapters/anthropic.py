"""AnthropicProvider: the Provider port over the anthropic SDK.

Layer: adapters — imports ports + kernel only. Every Anthropic specific is
quarantined here: SSE block-lifecycle buffering (the kernel sees ONE completed
ModelOutput, never deltas), string-aware streaming-JSON repair for truncated
tool args, PromptSection stability -> cache_control breakpoints, Effort ->
thinking budgets, and OAuth vs API-key dispatch including system-as-user
injection. Transient faults raise TransientProviderError; everything else
FatalProviderError — no internal retry, drive/retry.py wraps the port.
Credentials and cache TTL arrive via the constructor; this module never reads
os.environ or probes the filesystem.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from forge.kernel.types import (
    AssistantMessage,
    Block,
    Delta,
    Effort,
    Message,
    ModelInfo,
    ModelOutput,
    ModelRequest,
    Pricing,
    PromptSection,
    Stability,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from forge.ports.provider import FatalProviderError, TransientProviderError

CLAUDE_CODE_VERSION = "2.1.75"
OAUTH_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_OAUTH_BETAS = ("claude-code-20250219", "oauth-2025-04-20")
_THINKING_BETA = "interleaved-thinking-2025-05-14"
_RETRY_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})
_THINKING_BUDGETS = {Effort.LOW: 1024, Effort.MED: 4096, Effort.HIGH: 16000}
_DEFAULT_MAX_OUTPUT = 8192


def _is_oauth(api_key: str) -> bool:
    return "sk-ant-oat" in api_key


# --- surrogate sanitization --------------------------------------------------

# Lone UTF-16 surrogates are valid in Python str but crash the SDK's UTF-8
# serialisation on certain source files; replace with U+FFFD on every
# outbound and inbound text surface.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub("�", text)


# --- string-aware streaming-JSON repair --------------------------------------


def _scan(raw: str) -> tuple[list[str], bool, bool]:
    """Walk raw JSON tracking (open-bracket stack, in_string, trailing escape)."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in raw:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}" and stack:
            stack.pop()
    return stack, in_string, escape


def _close(raw: str) -> str:
    """Close an unterminated string, then unwind the open-bracket stack."""
    stack, in_string, escape = _scan(raw)
    out = raw[:-1] if escape else raw
    if in_string:
        out += '"'
    return out + "".join("}" if opener == "{" else "]" for opener in reversed(stack))


def _safe_cuts(raw: str) -> list[int]:
    """Positions of commas outside strings — points where a partial trailing
    member (e.g. a dangling key) can be dropped wholesale."""
    cuts: list[int] = []
    in_string = False
    escape = False
    for i, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == ",":
            cuts.append(i)
    return cuts


def _repair_json(raw: str) -> dict[str, Any]:
    """Parse possibly-truncated streaming JSON with progressive repair.

    Falls back to {} so tools receive clean missing-arg errors rather than a
    surprise key crashing the call. Bracket balancing is string-aware: braces
    inside string values never miscount.
    """
    if not raw or raw.isspace():
        return {}

    def attempts() -> Any:
        yield raw
        yield _close(raw)
        for cut in reversed(_safe_cuts(raw)):
            yield _close(raw[:cut])

    for candidate in attempts():
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --- static model fallback table ----------------------------------------------

_ALL_EFFORTS = frozenset(Effort)
_NO_THINKING = frozenset({Effort.NONE})


@dataclass(frozen=True)
class _Entry:
    info: ModelInfo
    max_output: int


def _entry(
    family: str,
    *,
    pricing: Pricing | None,
    efforts: frozenset[Effort] = _ALL_EFFORTS,
    context: int = 200_000,
    max_output: int = 64_000,
) -> tuple[str, _Entry]:
    return family, _Entry(
        info=ModelInfo(id=family, context_tokens=context, pricing=pricing, efforts=efforts),
        max_output=max_output,
    )


# Ordered most-specific-first; matched by substring against the request's
# model id so dated ids ("claude-sonnet-4-5-20250929") resolve to a family.
# pricing=None on known-but-unpriced families: report tokens, omit cost.
_MODEL_TABLE: tuple[tuple[str, _Entry], ...] = (
    _entry("claude-opus-4-7", pricing=None),
    _entry("claude-opus-4-6", pricing=None),
    _entry("claude-opus-4-5", pricing=Pricing(5.0, 25.0, 0.5, 6.25)),
    _entry("claude-opus-4-1", pricing=Pricing(15.0, 75.0, 1.5, 18.75), max_output=32_000),
    _entry("claude-opus-4", pricing=Pricing(15.0, 75.0, 1.5, 18.75), max_output=32_000),
    _entry("claude-sonnet-4-6", pricing=None),
    _entry("claude-sonnet-4-5", pricing=Pricing(3.0, 15.0, 0.3, 3.75)),
    _entry("claude-sonnet-4", pricing=Pricing(3.0, 15.0, 0.3, 3.75)),
    _entry("claude-haiku-4-5", pricing=Pricing(1.0, 5.0, 0.1, 1.25)),
    _entry("claude-3-5-haiku", pricing=Pricing(0.8, 4.0, 0.08, 1.0), efforts=_NO_THINKING, max_output=8192),
)


def _lookup(model: str) -> _Entry | None:
    for family, entry in _MODEL_TABLE:
        if family in model:
            return entry
    return None


def _fallback_info(model: str) -> ModelInfo:
    # Unknown model: report tokens, omit cost, and never send thinking params
    # it might reject — Effort degrades to NONE rather than erroring fatally.
    return ModelInfo(id=model, context_tokens=200_000, pricing=None, efforts=_NO_THINKING)


# --- request building ----------------------------------------------------------


def _system_blocks(
    sections: tuple[PromptSection, ...], cache_ctrl: dict[str, str]
) -> list[dict[str, Any]]:
    """Stability tags -> cache_control breakpoints: the last section of each
    contiguous stability group gets one, except VOLATILE (it changes every
    call, so a breakpoint there only buys cache-write cost)."""
    kept = [s for s in sections if s.text.strip()]
    blocks: list[dict[str, Any]] = []
    for i, section in enumerate(kept):
        block: dict[str, Any] = {"type": "text", "text": _sanitize_surrogates(section.text)}
        last_of_group = i + 1 == len(kept) or kept[i + 1].stability is not section.stability
        if last_of_group and section.stability is not Stability.VOLATILE:
            block["cache_control"] = cache_ctrl
        blocks.append(block)
    return blocks


def _to_api_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": _sanitize_surrogates(msg.text)})
        elif isinstance(msg, AssistantMessage):
            content: list[dict[str, Any]] = []
            for blk in msg.blocks:
                if isinstance(blk, TextBlock):
                    content.append({"type": "text", "text": _sanitize_surrogates(blk.text)})
                elif isinstance(blk, ToolCall):
                    content.append(
                        {"type": "tool_use", "id": blk.id, "name": blk.name, "input": dict(blk.args)}
                    )
                # ThinkingBlock is dropped on replay: the kernel carries no
                # signature and the API rejects unsigned thinking blocks.
            if content:
                out.append({"role": "assistant", "content": content})
        elif isinstance(msg, ToolResultMessage):
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": _sanitize_surrogates(r.content),
                            "is_error": r.is_error,
                        }
                        for r in msg.results
                    ],
                }
            )
    return out


def _stamp_last_user(msgs: list[dict[str, Any]], cache_ctrl: dict[str, str]) -> None:
    """cache_control on the last user message so the whole prior conversation
    is served from cache on the next turn."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") != "user":
            continue
        content = msgs[i]["content"]
        if isinstance(content, str):
            msgs[i] = {
                **msgs[i],
                "content": [{"type": "text", "text": content, "cache_control": cache_ctrl}],
            }
        elif isinstance(content, list) and content:
            last = dict(content[-1])
            last["cache_control"] = cache_ctrl
            msgs[i] = {**msgs[i], "content": [*content[:-1], last]}
        return


def _api_tools(tools: tuple[ToolSpec, ...], cache_ctrl: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, tool in enumerate(tools):
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "input_schema": dict(tool.params) or {"type": "object", "properties": {}},
            # lets the API start processing tool args before the full JSON
            # delta arrives — saves ~100-300 ms per tool call
            "eager_input_streaming": True,
        }
        if i == len(tools) - 1:
            entry["cache_control"] = cache_ctrl
        out.append(entry)
    return out


def _system_already_injected(api_msgs: list[dict[str, Any]], real_system: str) -> bool:
    if not api_msgs or api_msgs[0].get("role") != "user":
        return False
    content = api_msgs[0].get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "text" and b.get("text") == real_system
        for b in content
    )


# --- streaming assembly ---------------------------------------------------------


@dataclass
class _BlockBuf:
    kind: str
    tool_id: str = ""
    tool_name: str = ""
    parts: list[str] = field(default_factory=list)

    def finish(self) -> Block | None:
        text = "".join(self.parts)
        if self.kind == "text":
            return TextBlock(_sanitize_surrogates(text))
        if self.kind == "thinking":
            return ThinkingBlock(text)
        if self.kind == "tool_use":
            return ToolCall(id=self.tool_id, name=self.tool_name, args=_repair_json(text))
        return None  # redacted_thinking etc. — nothing the kernel can hold


class AnthropicProvider:
    """Provider over the anthropic SDK; stateless across complete() calls."""

    def __init__(
        self,
        api_key: str,
        *,
        cache_ttl: str | None = None,  # "5m" | "1h"; None -> SDK default ephemeral
        max_tokens: int | None = None,  # None -> per-model table value
        client: Any | None = None,  # test seam: SDK-shaped client, no network
    ) -> None:
        self._api_key = api_key
        self._oauth = _is_oauth(api_key)
        self._cache_ttl = cache_ttl
        self._max_tokens = max_tokens
        self._client_obj = client

    # -- Provider port ----------------------------------------------------------

    async def complete(
        self,
        req: ModelRequest,
        on_delta: Callable[[Delta], None] | None = None,
    ) -> ModelOutput:
        kwargs = self._build_kwargs(req)
        try:
            return await self._stream(kwargs, on_delta)
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise TransientProviderError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in _RETRY_CODES:
                raise TransientProviderError(f"http {exc.status_code}: {exc}") from exc
            raise FatalProviderError(f"http {exc.status_code}: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise FatalProviderError(str(exc)) from exc
        except (TransientProviderError, FatalProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 — port contract: only the two error types escape
            raise FatalProviderError(f"{type(exc).__name__}: {exc}") from exc

    async def info(self, model: str) -> ModelInfo:
        entry = _lookup(model)
        if entry is None:
            return _fallback_info(model)
        # echo the requested id, not the family key, so callers can round-trip
        return ModelInfo(
            id=model,
            context_tokens=entry.info.context_tokens,
            pricing=entry.info.pricing,
            efforts=entry.info.efforts,
        )

    # -- internals ---------------------------------------------------------------

    def _client(self) -> Any:
        if self._client_obj is None:
            if self._oauth:
                self._client_obj = anthropic.AsyncAnthropic(
                    auth_token=self._api_key,
                    default_headers={
                        "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                        "x-app": "cli",
                        "anthropic-dangerous-direct-browser-access": "true",
                    },
                )
            else:
                self._client_obj = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client_obj

    def _cache_ctrl(self) -> dict[str, str]:
        ctrl = {"type": "ephemeral"}
        if self._cache_ttl is not None:
            ctrl["ttl"] = self._cache_ttl
        return ctrl

    def _build_kwargs(self, req: ModelRequest) -> dict[str, Any]:
        cache_ctrl = self._cache_ctrl()
        betas: list[str] = list(_OAUTH_BETAS) if self._oauth else []

        messages = _to_api_messages(req.messages)
        if self._oauth:
            # OAuth blocks arbitrary system= content; identity goes there and
            # the real system prompt is injected as the first user message.
            system_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": OAUTH_IDENTITY, "cache_control": cache_ctrl}
            ]
            real_system = "\n\n".join(s.text for s in req.system if s.text.strip())
            if real_system and not _system_already_injected(messages, real_system):
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": real_system, "cache_control": cache_ctrl}
                        ],
                    },
                    *messages,
                ]
        else:
            system_blocks = _system_blocks(req.system, cache_ctrl)
        _stamp_last_user(messages, cache_ctrl)

        entry = _lookup(req.model)
        info = entry.info if entry else _fallback_info(req.model)
        model_cap = entry.max_output if entry else _DEFAULT_MAX_OUTPUT
        max_tokens = self._max_tokens or model_cap

        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if req.tools:
            kwargs["tools"] = _api_tools(req.tools, cache_ctrl)

        if req.effort is not Effort.NONE and req.effort in info.efforts:
            budget = _THINKING_BUDGETS[req.effort]
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # max_tokens must exceed the thinking budget; grow it, capped at
            # the model's output ceiling
            kwargs["max_tokens"] = min(max_tokens + budget, max(model_cap, budget + 1))
            betas.append(_THINKING_BETA)

        if betas:
            kwargs["extra_headers"] = {"anthropic-beta": ",".join(betas)}
        return kwargs

    async def _stream(
        self, kwargs: dict[str, Any], on_delta: Callable[[Delta], None] | None
    ) -> ModelOutput:
        bufs: dict[int, _BlockBuf] = {}
        done: dict[int, Block] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        stop_reason = "end_turn"

        def merge_usage(raw: Any) -> None:
            for attr, key in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_input_tokens", "cache_read"),
                ("cache_creation_input_tokens", "cache_write"),
            ):
                value = getattr(raw, attr, None)
                if isinstance(value, int):
                    usage[key] = value

        def finish(index: int) -> None:
            buf = bufs.pop(index, None)
            if buf is None:
                return
            block = buf.finish()
            if block is not None:
                done[index] = block

        async with self._client().messages.stream(**kwargs) as stream:
            async for raw in stream:
                etype = getattr(raw, "type", "")
                if etype == "message_start":
                    merge_usage(getattr(getattr(raw, "message", None), "usage", None))
                elif etype == "content_block_start":
                    block = raw.content_block
                    kind = getattr(block, "type", "")
                    bufs[raw.index] = _BlockBuf(
                        kind=kind,
                        tool_id=getattr(block, "id", "") or "",
                        tool_name=getattr(block, "name", "") or "",
                    )
                elif etype == "content_block_delta":
                    delta = raw.delta
                    buf = bufs.get(raw.index)
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        text = _sanitize_surrogates(delta.text)
                        if buf is not None:
                            buf.parts.append(text)
                        if on_delta is not None:
                            on_delta(Delta("text", text))
                    elif dtype == "thinking_delta":
                        if buf is not None:
                            buf.parts.append(delta.thinking)
                        if on_delta is not None:
                            on_delta(Delta("thinking", delta.thinking))
                    elif dtype == "input_json_delta" and buf is not None:
                        buf.parts.append(delta.partial_json)
                    # signature_delta etc.: nothing the kernel can hold
                elif etype == "content_block_stop":
                    finish(raw.index)
                elif etype == "message_delta":
                    reason = getattr(getattr(raw, "delta", None), "stop_reason", None)
                    if reason:
                        stop_reason = reason
                    merge_usage(getattr(raw, "usage", None))

        for index in sorted(bufs):  # blocks never closed by the stream
            finish(index)

        return ModelOutput(
            blocks=tuple(done[i] for i in sorted(done)),
            usage=Usage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read"],
                cache_write_tokens=usage["cache_write"],
            ),
            stop_reason=stop_reason,
        )
