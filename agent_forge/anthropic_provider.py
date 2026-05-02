"""
anthropic_provider.py — AnthropicProvider adapter (the only file that knows
about anthropic-SDK wire format, beta headers, OAuth quirks, cache_control
blocks, and Anthropic's streaming event shapes).

Depends on messages, models, and provider (LLMProvider Protocol + StreamEvent
union). Nothing in agent_forge imports back into this module: it is the
implementation side of an Anthropic-specific port. Replacing it with a
different LLMProvider (OpenAI, local Ollama, fake) does not require changes
anywhere else in the package.

Owns: AnthropicProvider (carries its own api_key; no key on stream() signature),
      _do_stream() (streams the SDK and translates raw events → StreamEvent),
      _to_api_messages() (Message → Anthropic API format with cache_control on
      the last user message, Fix 9), _tool_result_block(), _build_assistant_msg(),
      _extract_usage() (cost calc), _system_already_injected() (OAuth idempotency),
      _supports_adaptive_thinking(), _is_oauth(), _sanitize_surrogates() (Fix 8),
      _repair_json() (Fix 7 — recover from truncated streaming tool args).

OAuth vs API key: when api_key starts with sk-ant-oat, we use the Claude-Code
OAuth flow which requires a fixed identity in the system prompt and beta
headers; the user-supplied system prompt is injected as the first user message.
For raw API keys we use the standard Messages API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from .messages import (
    AssistantMessage, ContentBlock, ImageContent, Message,
    SystemPromptSection, TextContent, ThinkingContent,
    ToolCallContent, ToolDefinition, ToolResultMessage, TokenUsage,
    UserMessage,
)
from .models import Model
from .provider import (
    ContentBlockEndEvent, ContentBlockStartEvent, DoneEvent,
    StreamErrorEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    ToolCallEndEvent,
)

logger = logging.getLogger(__name__)

CLAUDE_CODE_VERSION = "2.1.75"
OAUTH_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_RETRY_CODES = {429, 500, 502, 503, 529}


def _is_oauth(api_key: str) -> bool:
    return "sk-ant-oat" in api_key


# ── Fix 8: surrogate sanitization ────────────────────────────────────────────

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates (U+D800–U+DFFF) with U+FFFD.

    Lone surrogates are valid in Python str but crash JSON / UTF-8 serialisation,
    which the Anthropic SDK hits on certain source files.
    """
    return _SURROGATE_RE.sub("\ufffd", text)


# ── Fix 7: streaming JSON repair ──────────────────────────────────────────────

def _repair_json(raw: str) -> dict:
    """Parse possibly-truncated streaming JSON with progressive repair.

    Falls back to {} (not {"_raw": ...}) so tools receive clean missing-arg
    errors rather than an unexpected _raw key crashing the call.
    """
    if not raw or raw.isspace():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Pass 1: balance open braces / brackets
    repaired = raw
    open_brackets = max(0, repaired.count("[") - repaired.count("]"))
    open_braces = max(0, repaired.count("{") - repaired.count("}"))
    repaired += "]" * open_brackets + "}" * open_braces
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # Pass 2: strip trailing incomplete token then re-balance
    for suffix in ('", ', '",', '"', ", ", ",", "\\"):
        t = raw.rstrip()
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            t += "]" * max(0, t.count("[") - t.count("]"))
            t += "}" * max(0, t.count("{") - t.count("}"))
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                continue
    return {}


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Sonnet 4.6 / Opus 4.6+ accept {type:"adaptive"} thinking — model self-budgets."""
    return any(tag in model_id for tag in ("sonnet-4-6", "opus-4-6", "opus-4-7"))


# ── AnthropicProvider ────────────────────────────────────────────────────────

class AnthropicProvider:
    """ACL: Anthropic Messages API → StreamEvent. Carries its own api_key."""

    def __init__(
        self,
        api_key: str | None = None,
        cwd: str = ".",
        project_root: str | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
        self._cwd = cwd
        # Fix 10: explicit project_root takes priority; falls back to cwd.
        # Autonomous mode passes cfg.repo_path so the worktree (a sibling of
        # the repo) still detects .agent-forge/ correctly.
        self._project_root = project_root or cwd

    async def stream(
        self,
        model: Model,
        system: list[SystemPromptSection],
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        signal: asyncio.Event | None = None,
        max_tokens: int | None = None,
        thinking: str = "off",
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        api_key = self._api_key
        is_oauth = _is_oauth(api_key)
        betas: list[str] = []

        # Fix 10: 1-hour TTL when running inside a project that has .agent-forge/.
        # Use project_root (set to repo_path by autonomous mode) so that worktrees,
        # which are sibling directories without .agent-forge/, are still detected.
        _project = Path(self._project_root, ".agent-forge").exists()
        _cache_ctrl: dict = (
            {"type": "ephemeral", "ttl": "1h"} if _project else {"type": "ephemeral"}
        )

        if is_oauth:
            client = _anthropic.AsyncAnthropic(
                auth_token=api_key,
                default_headers={
                    "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                    "x-app": "cli",
                    "anthropic-dangerous-direct-browser-access": "true",
                },
            )
            betas.extend(["claude-code-20250219", "oauth-2025-04-20"])
            system_blocks = [{"type": "text", "text": OAUTH_IDENTITY, "cache_control": _cache_ctrl}]
            real_system = "\n\n".join(s.text for s in system if s.text.strip())
        else:
            client = _anthropic.AsyncAnthropic(api_key=api_key)
            system_blocks = [
                {
                    "type": "text", "text": s.text,
                    **({"cache_control": _cache_ctrl} if s.cache_control else {}),
                }
                for s in system if s.text.strip()
            ]
            real_system = ""

        # Fix 9: stamp cache_control on the last user message so the full
        # prior conversation is served from cache on the next turn.
        api_msgs = _to_api_messages(messages, cache_last=True, last_cache_ctrl=_cache_ctrl)

        # OAuth: inject system content as the first user message with cache_control.
        # Done at the api_msgs level so UserMessage stays clean (no cached= field).
        if is_oauth and real_system and not _system_already_injected(api_msgs, real_system):
            api_msgs = [{
                "role": "user",
                "content": [{"type": "text", "text": real_system, "cache_control": _cache_ctrl}],
            }] + api_msgs

        # Fix 11: eager_input_streaming lets the API begin processing tool args
        # before the full JSON delta arrives, saving ~100-300 ms per tool call.
        api_tools: list[dict] = []
        for i, t in enumerate(tools):
            entry: dict = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
                "eager_input_streaming": True,
            }
            if i == len(tools) - 1:
                entry["cache_control"] = _cache_ctrl
            api_tools.append(entry)

        # Thinking-mode routing:
        #   "adaptive" → {type:"adaptive"} on supported models (Sonnet 4.6 / Opus 4.6+),
        #               where the model self-budgets. Note: adaptive returns the reasoning
        #               as an opaque signed block — no thinking_delta events stream down.
        #   low/medium/high → {type:"enabled", budget_tokens:N}. This emits live
        #               thinking_delta events on every supported model. Honour the user's
        #               explicit level regardless of whether the model also supports adaptive.
        #   "off" → no thinking field sent.
        thinking_param: dict | _anthropic.NotGiven = _anthropic.NOT_GIVEN
        if thinking != "off":
            if thinking == "adaptive":
                if _supports_adaptive_thinking(model.id):
                    thinking_param = {"type": "adaptive"}
                else:
                    thinking_param = {"type": "enabled", "budget_tokens": 4096}
            else:
                budget = {"low": 1024, "medium": 4096, "high": 16000}.get(thinking, 4096)
                thinking_param = {"type": "enabled", "budget_tokens": budget}
            betas.append("interleaved-thinking-2025-05-14")

        effective_max = max_tokens or model.max_tokens
        if isinstance(thinking_param, dict) and thinking_param.get("type") == "enabled":
            effective_max = min(
                effective_max + thinking_param.get("budget_tokens", 0),
                model.max_tokens,
            )

        # No retry loop here — retries are owned by loop._stream_one_turn().
        # Classify errors and surface them; the loop decides if/when to retry.
        try:
            async for ev in _do_stream(
                client, model, system_blocks, api_msgs, api_tools,
                effective_max, thinking_param, betas, signal,
            ):
                yield ev
        except _anthropic.RateLimitError as exc:
            yield StreamErrorEvent(error=str(exc), retryable=True)
        except _anthropic.APIStatusError as exc:
            yield StreamErrorEvent(
                error=str(exc),
                retryable=exc.status_code in _RETRY_CODES,
            )
        except Exception as exc:
            yield StreamErrorEvent(error=str(exc), retryable=False)


def _system_already_injected(api_msgs: list[dict], real_system: str) -> bool:
    """Check if the first api message is already the OAuth-injected system content."""
    if not api_msgs:
        return False
    first = api_msgs[0]
    if first.get("role") != "user":
        return False
    content = first.get("content", [])
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text") == real_system
            for b in content
        )
    return False


async def _do_stream(
    client: object,
    model: Model,
    system_blocks: list[dict],
    api_msgs: list[dict],
    api_tools: list[dict],
    max_tokens: int,
    thinking_param: object,
    betas: list[str],
    signal: asyncio.Event | None,
) -> AsyncIterator[StreamEvent]:
    import anthropic as _anthropic

    tool_arg_bufs: dict[str, str] = {}
    content_blocks: list = []

    # Diagnostic tap: when AGENT_FORGE_DEBUG_STREAM=1, log every raw SDK event
    # with a monotonic timestamp. Toggle via `agent-forge --debug-stream`.
    _debug = os.environ.get("AGENT_FORGE_DEBUG_STREAM") == "1"
    _t0 = time.monotonic()

    def _dbg(label: str) -> None:
        if not _debug:
            return
        dt = time.monotonic() - _t0
        print(f"\x1b[2m[stream t+{dt:7.3f}s] {label}\x1b[0m", file=sys.stderr, flush=True)

    kwargs: dict = {
        "model": model.id,
        "max_tokens": max_tokens,
        "messages": api_msgs,
    }
    if system_blocks:
        kwargs["system"] = system_blocks
    if api_tools:
        kwargs["tools"] = api_tools
    if thinking_param is not _anthropic.NOT_GIVEN:
        kwargs["thinking"] = thinking_param
    if betas:
        kwargs["extra_headers"] = {"anthropic-beta": ",".join(betas)}

    _dbg(f"stream open  model={model.id} thinking={thinking_param!r}")
    async with client.messages.stream(**kwargs) as stream:  # type: ignore[attr-defined]
        async for raw in stream:
            if signal and signal.is_set():
                _dbg("aborted by signal")
                return

            etype = raw.type

            if etype == "content_block_start":
                block = raw.content_block
                _dbg(f"content_block_start  idx={raw.index} type={block.type}")
                content_blocks.append(block)
                if block.type == "tool_use":
                    tool_arg_bufs[block.id] = ""
                # Emit block-start event for all block types
                yield ContentBlockStartEvent(
                    index=raw.index,
                    block_type=block.type,
                    tool_id=getattr(block, "id", None),
                    tool_name=getattr(block, "name", None),
                )

            elif etype == "content_block_delta":
                delta = raw.delta
                if delta.type == "text_delta":
                    _dbg(f"text_delta           idx={raw.index} len={len(delta.text)}")
                    yield TextDeltaEvent(delta=_sanitize_surrogates(delta.text))  # Fix 8
                elif delta.type == "thinking_delta":
                    _dbg(f"thinking_delta       idx={raw.index} len={len(delta.thinking)}")
                    yield ThinkingDeltaEvent(delta=delta.thinking)
                elif delta.type == "input_json_delta":
                    _dbg(f"input_json_delta     idx={raw.index} len={len(delta.partial_json)}")
                    blk = content_blocks[raw.index]
                    tool_arg_bufs[blk.id] += delta.partial_json
                else:
                    _dbg(f"delta(other)         idx={raw.index} type={delta.type}")

            elif etype == "content_block_stop":
                _dbg(f"content_block_stop   idx={raw.index}")
                if raw.index < len(content_blocks):
                    blk = content_blocks[raw.index]
                    if blk.type == "tool_use":
                        # ToolCallEndEvent carries the fully-parsed args
                        raw_args = tool_arg_bufs.get(blk.id, "{}")
                        args = _repair_json(raw_args)  # Fix 7: repair truncated streaming JSON
                        yield ToolCallEndEvent(id=blk.id, name=blk.name, arguments=args)
                    else:
                        # text / thinking — emit ContentBlockEndEvent
                        yield ContentBlockEndEvent(index=raw.index, block_type=blk.type)

            elif etype == "message_stop":
                _dbg("message_stop")
                final = await stream.get_final_message()
                usage = _extract_usage(final.usage, model)
                assistant = _build_assistant_msg(
                    final.content,
                    tool_arg_bufs,
                    stop_reason=getattr(final, "stop_reason", "end_turn"),
                    usage=usage,
                    model_id=model.id,
                )
                yield DoneEvent(message=assistant)

            else:
                _dbg(f"raw(other)           type={etype}")


def _extract_usage(raw: object, model: Model) -> TokenUsage:
    inp = getattr(raw, "input_tokens", 0)
    out = getattr(raw, "output_tokens", 0)
    cw  = getattr(raw, "cache_creation_input_tokens", 0)
    cr  = getattr(raw, "cache_read_input_tokens", 0)
    cost = (
        inp * model.cost.input / 1_000_000
        + out * model.cost.output / 1_000_000
        + cw  * model.cost.cache_write / 1_000_000
        + cr  * model.cost.cache_read / 1_000_000
    )
    return TokenUsage(input=inp, output=out, cache_read=cr, cache_write=cw, cost=cost)


def _build_assistant_msg(
    content_blocks: list,
    tool_arg_bufs: dict[str, str],
    stop_reason: str = "end_turn",
    usage: TokenUsage | None = None,
    model_id: str | None = None,
) -> AssistantMessage:
    blocks: list[ContentBlock] = []
    for blk in content_blocks:
        if blk.type == "text":
            blocks.append(TextContent(text=_sanitize_surrogates(blk.text)))  # Fix 8
        elif blk.type == "thinking":
            blocks.append(ThinkingContent(thinking=blk.thinking, signature=getattr(blk, "signature", None)))
        elif blk.type == "tool_use":
            raw = tool_arg_bufs.get(blk.id, "{}")
            args = _repair_json(raw)  # Fix 7
            blocks.append(ToolCallContent(id=blk.id, name=blk.name, arguments=args))
    return AssistantMessage(
        content=tuple(blocks),
        stop_reason=stop_reason,
        usage=usage,
        model_id=model_id,
    )


def _to_api_messages(
    messages: list[Message],
    *,
    cache_last: bool = False,
    last_cache_ctrl: dict | None = None,
) -> list[dict]:
    """Convert internal Message types to Anthropic API format, merging tool results.

    Fix 9: when cache_last=True, stamps cache_control on the last user message
    so the full prior conversation is read from cache on every subsequent turn.
    """
    result: list[dict] = []
    pending_tool_results: list[ToolResultMessage] = []

    def flush_tools() -> None:
        if not pending_tool_results:
            return
        result.append({
            "role": "user",
            "content": [
                _tool_result_block(r)
                for r in pending_tool_results
            ],
        })
        pending_tool_results.clear()

    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            pending_tool_results.append(msg)
            continue

        flush_tools()

        if isinstance(msg, UserMessage):
            if isinstance(msg.content, str):
                result.append({"role": "user", "content": _sanitize_surrogates(msg.content)})  # Fix 8
            else:
                result.append({"role": "user", "content": [{"type": "text", "text": _sanitize_surrogates(c.text)} for c in msg.content]})  # Fix 8

        elif isinstance(msg, AssistantMessage):
            api_content: list[dict] = []
            for blk in msg.content:
                if isinstance(blk, TextContent):
                    api_content.append({"type": "text", "text": _sanitize_surrogates(blk.text)})  # Fix 8
                elif isinstance(blk, ThinkingContent):
                    entry: dict = {"type": "thinking", "thinking": blk.thinking}
                    if blk.signature:
                        entry["signature"] = blk.signature
                    api_content.append(entry)
                elif isinstance(blk, ToolCallContent):
                    api_content.append({"type": "tool_use", "id": blk.id, "name": blk.name, "input": blk.arguments})
            result.append({"role": "assistant", "content": api_content})

    flush_tools()

    # Fix 9: cache_control on the last user message — covers all prior context.
    if cache_last and result:
        ctrl = last_cache_ctrl or {"type": "ephemeral"}
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") == "user":
                content = msg["content"]
                if isinstance(content, str):
                    result[i] = {**msg, "content": [{"type": "text", "text": content, "cache_control": ctrl}]}
                elif isinstance(content, list) and content:
                    last_block = dict(content[-1])
                    last_block["cache_control"] = ctrl
                    result[i] = {**msg, "content": list(content[:-1]) + [last_block]}
                break

    return result


def _tool_result_block(r: ToolResultMessage) -> dict:
    """Build a single tool_result API block, supporting str or structured content."""
    if isinstance(r.content, str):
        content: str | list = _sanitize_surrogates(r.content)  # Fix 8
    else:
        content = []
        for blk in r.content:
            if isinstance(blk, TextContent):
                content.append({"type": "text", "text": blk.text})
            elif isinstance(blk, ImageContent):
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": blk.media_type, "data": blk.data},
                })
    return {
        "type": "tool_result",
        "tool_use_id": r.tool_call_id,
        "content": content,
        "is_error": r.is_error,
    }
