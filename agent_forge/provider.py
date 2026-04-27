"""
provider.py — shared message types, LLMProvider protocol, AnthropicProvider adapter.

Leaf dependency: imports nothing from agent_forge. Every other module imports
from here. This is the only file that knows about Anthropic-specific wire formats
(beta headers, cache_control blocks, streaming event shapes).

Owns: UserMessage / AssistantMessage / ToolResultMessage, content block types
      (TextContent / ThinkingContent / ToolCallContent), TokenUsage, ToolResult,
      ToolDefinition, SystemPromptSection, Model catalog, AnthropicProvider.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

CLAUDE_CODE_VERSION = "2.1.75"
OAUTH_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

def _is_oauth(api_key: str) -> bool:
    return "sk-ant-oat" in api_key

# ── Content blocks ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TextContent:
    text: str
    type: Literal["text"] = field(default="text", init=False)

@dataclass(frozen=True)
class ThinkingContent:
    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = field(default="thinking", init=False)

@dataclass(frozen=True)
class ToolCallContent:
    id: str
    name: str
    arguments: dict
    type: Literal["tool_use"] = field(default="tool_use", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", dict(self.arguments))

ContentBlock = TextContent | ThinkingContent | ToolCallContent

# ── Messages ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UserMessage:
    content: str | tuple[TextContent, ...]
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    cached: bool = False   # if True, wraps content in a block with cache_control
    role: Literal["user"] = field(default="user", init=False)

@dataclass(frozen=True)
class AssistantMessage:
    content: tuple[ContentBlock, ...]
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["assistant"] = field(default="assistant", init=False)

@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    content: str
    is_error: bool = False
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    role: Literal["tool_result"] = field(default="tool_result", init=False)

Message = UserMessage | AssistantMessage | ToolResultMessage

# ── Token economics ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost=self.cost + other.cost,
        )

ZERO_USAGE = TokenUsage()

# ── Tool plumbing ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict

# ── Model descriptor ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelCost:
    input: float   # $ per 1M tokens
    output: float
    cache_read: float
    cache_write: float

@dataclass(frozen=True)
class Model:
    id: str
    context_window: int
    max_tokens: int
    reasoning: bool
    cost: ModelCost

    @classmethod
    def from_id(cls, model_id: str) -> Model:
        if model_id not in MODELS:
            raise ValueError(f"Unknown model: {model_id!r}. Known: {list(MODELS)}")
        return MODELS[model_id]

_S46 = ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)
_S45 = ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)
_O47 = ModelCost(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25)
_H45 = ModelCost(input=1.0, output=5.0,  cache_read=0.10, cache_write=1.25)

MODELS: dict[str, Model] = {
    "claude-sonnet-4-6": Model("claude-sonnet-4-6", 1_000_000, 64_000, True, _S46),
    "claude-sonnet-4-5": Model("claude-sonnet-4-5",   200_000, 64_000, True, _S45),
    "claude-haiku-4-5":  Model("claude-haiku-4-5",    200_000, 64_000, False, _H45),
    "claude-opus-4-7":   Model("claude-opus-4-7",   1_000_000, 128_000, True, _O47),
}
DEFAULT_MODEL = MODELS["claude-sonnet-4-6"]

# ── Stream events ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TextDeltaEvent:
    delta: str

@dataclass(frozen=True)
class ThinkingDeltaEvent:
    delta: str

@dataclass(frozen=True)
class ToolCallEndEvent:
    id: str
    name: str
    arguments: dict

@dataclass(frozen=True)
class DoneEvent:
    message: AssistantMessage
    usage: TokenUsage

@dataclass(frozen=True)
class StreamErrorEvent:
    error: str
    retryable: bool

StreamEvent = TextDeltaEvent | ThinkingDeltaEvent | ToolCallEndEvent | DoneEvent | StreamErrorEvent

# ── System prompt section ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemPromptSection:
    text: str
    cache_control: bool = False

# ── LLMProvider Protocol ──────────────────────────────────────────────────────

class LLMProvider(Protocol):
    async def stream(
        self,
        model: Model,
        system: list[SystemPromptSection],
        messages: list[Message],
        tools: list[ToolDefinition],
        api_key: str,
        *,
        signal: asyncio.Event | None = None,
        max_tokens: int | None = None,
        thinking: str = "off",
    ) -> AsyncIterator[StreamEvent]: ...

# ── Anthropic adapter ─────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_CODES = {429, 500, 502, 503, 529}


class AnthropicProvider:
    """ACL: Anthropic Messages API → StreamEvent. The only Anthropic-aware code."""

    async def stream(
        self,
        model: Model,
        system: list[SystemPromptSection],
        messages: list[Message],
        tools: list[ToolDefinition],
        api_key: str,
        *,
        signal: asyncio.Event | None = None,
        max_tokens: int | None = None,
        thinking: str = "off",
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        is_oauth = _is_oauth(api_key)
        betas: list[str] = []

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
            system_blocks = [{"type": "text", "text": OAUTH_IDENTITY, "cache_control": {"type": "ephemeral"}}]
            real_system = "\n\n".join(s.text for s in system if s.text.strip())
            if real_system and not _system_already_injected(messages, real_system):
                messages = [UserMessage(content=real_system, cached=True)] + list(messages)
        else:
            client = _anthropic.AsyncAnthropic(api_key=api_key)
            system_blocks = [
                {"type": "text", "text": s.text,
                 **({"cache_control": {"type": "ephemeral"}} if s.cache_control else {})}
                for s in system if s.text.strip()
            ]

        api_msgs = _to_api_messages(messages)

        api_tools: list[dict] = []
        for i, t in enumerate(tools):
            entry: dict = {"name": t.name, "description": t.description, "input_schema": t.parameters}
            if i == len(tools) - 1:
                entry["cache_control"] = {"type": "ephemeral"}
            api_tools.append(entry)

        thinking_param: dict | _anthropic.NotGiven = _anthropic.NOT_GIVEN
        if thinking == "adaptive":
            thinking_param = {"type": "adaptive"}
            betas.append("interleaved-thinking-2025-05-14")
        elif thinking != "off":
            budget = {"low": 1024, "medium": 4096, "high": 16000}.get(thinking, 4096)
            thinking_param = {"type": "enabled", "budget_tokens": budget}
            betas.append("interleaved-thinking-2025-05-14")

        effective_max = max_tokens or model.max_tokens
        if isinstance(thinking_param, dict) and thinking_param.get("type") == "enabled":
            effective_max = min(effective_max + thinking_param.get("budget_tokens", 0), model.max_tokens)

        for attempt in range(_MAX_RETRIES):
            try:
                async for ev in _do_stream(
                    client, model, system_blocks, api_msgs, api_tools,
                    effective_max, thinking_param, betas, signal,
                ):
                    yield ev
                return
            except _anthropic.RateLimitError as exc:
                retry_after = float(getattr(exc.response, "headers", {}).get("retry-after", 5))
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(retry_after)
                    continue
                yield StreamErrorEvent(error=str(exc), retryable=True)
                return
            except _anthropic.APIStatusError as exc:
                if exc.status_code in _RETRY_CODES and attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                yield StreamErrorEvent(error=str(exc), retryable=exc.status_code in _RETRY_CODES)
                return
            except Exception as exc:
                yield StreamErrorEvent(error=str(exc), retryable=False)
                return


def _system_already_injected(messages: list[Message], real_system: str) -> bool:
    """Guard: check if first message is already the injected OAuth system content."""
    if not messages:
        return False
    first = messages[0]
    if not isinstance(first, UserMessage):
        return False
    content = first.content if isinstance(first.content, str) else ""
    return content == real_system


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

    async with client.messages.stream(**kwargs) as stream:  # type: ignore[attr-defined]
        async for raw in stream:
            if signal and signal.is_set():
                return

            etype = raw.type

            if etype == "content_block_start":
                block = raw.content_block
                content_blocks.append(block)
                if block.type == "tool_use":
                    tool_arg_bufs[block.id] = ""

            elif etype == "content_block_delta":
                delta = raw.delta
                if delta.type == "text_delta":
                    yield TextDeltaEvent(delta=delta.text)
                elif delta.type == "thinking_delta":
                    yield ThinkingDeltaEvent(delta=delta.thinking)
                elif delta.type == "input_json_delta":
                    blk = content_blocks[raw.index]
                    tool_arg_bufs[blk.id] += delta.partial_json

            elif etype == "content_block_stop":
                if raw.index < len(content_blocks):
                    blk = content_blocks[raw.index]
                    if blk.type == "tool_use":
                        raw_args = tool_arg_bufs.get(blk.id, "{}")
                        try:
                            args = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            args = {"_raw": raw_args}
                        yield ToolCallEndEvent(id=blk.id, name=blk.name, arguments=args)

            elif etype == "message_stop":
                final = await stream.get_final_message()
                usage = _extract_usage(final.usage, model)
                assistant = _build_assistant_msg(final.content, tool_arg_bufs)
                yield DoneEvent(message=assistant, usage=usage)


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


def _build_assistant_msg(content_blocks: list, tool_arg_bufs: dict[str, str]) -> AssistantMessage:
    blocks: list[ContentBlock] = []
    for blk in content_blocks:
        if blk.type == "text":
            blocks.append(TextContent(text=blk.text))
        elif blk.type == "thinking":
            blocks.append(ThinkingContent(thinking=blk.thinking, signature=getattr(blk, "signature", None)))
        elif blk.type == "tool_use":
            raw = tool_arg_bufs.get(blk.id, "{}")
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {}
            blocks.append(ToolCallContent(id=blk.id, name=blk.name, arguments=args))
    return AssistantMessage(content=tuple(blocks))


def _to_api_messages(messages: list[Message]) -> list[dict]:
    """Convert internal Message types to Anthropic API format, merging tool results."""
    result: list[dict] = []
    pending_tool_results: list[ToolResultMessage] = []

    def flush_tools() -> None:
        if not pending_tool_results:
            return
        result.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
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
                if msg.cached:
                    result.append({"role": "user", "content": [
                        {"type": "text", "text": msg.content,
                         "cache_control": {"type": "ephemeral"}}
                    ]})
                else:
                    result.append({"role": "user", "content": msg.content})
            else:
                result.append({"role": "user", "content": [{"type": "text", "text": c.text} for c in msg.content]})

        elif isinstance(msg, AssistantMessage):
            api_content: list[dict] = []
            for blk in msg.content:
                if isinstance(blk, TextContent):
                    api_content.append({"type": "text", "text": blk.text})
                elif isinstance(blk, ThinkingContent):
                    entry: dict = {"type": "thinking", "thinking": blk.thinking}
                    if blk.signature:
                        entry["signature"] = blk.signature
                    api_content.append(entry)
                elif isinstance(blk, ToolCallContent):
                    api_content.append({"type": "tool_use", "id": blk.id, "name": blk.name, "input": blk.arguments})
            result.append({"role": "assistant", "content": api_content})

    flush_tools()
    return result
