"""
wiki/_llm.py — minimal one-shot LLM harness for compile / ratchet / compact.

Private helper: wraps a ``LLMProvider`` so the LLM-using wiki stages can call
the model with a system prompt + user message + no tools, and get back a
single string. Drains TextDeltaEvent until DoneEvent / StreamErrorEvent.

This is deliberately *not* the full agent loop. The wiki stages don't need
tool calling, retries, or streaming UX — they need "ask the model, get text
back, save text to disk". Reusing ``loop.agent_loop`` would drag the whole
loop machinery in for one-shot prompts.

Public:  one_shot(provider, model, system_prompt, user, *, max_tokens) -> str

Lazy import of AnthropicProvider lets the wiki subsystem stay importable
without the Anthropic SDK installed (matches __init__ best-effort pattern).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..messages import AssistantMessage, SystemPromptSection, TextContent, UserMessage
from ..models import Model
from ..provider import (
    DoneEvent, LLMProvider, StreamErrorEvent, TextDeltaEvent,
)


@dataclass(frozen=True)
class LLMCallResult:
    text: str
    aborted: bool = False
    error: str | None = None


async def one_shot(
    provider: LLMProvider,
    model: Model,
    system: str,
    user: str,
    *,
    max_tokens: int = 4096,
    timeout_seconds: int = 120,
) -> LLMCallResult:
    """Call the LLM once with no tools. Return the assembled text response.

    On error returns an empty-text result with ``error`` set; never raises.
    Caller decides whether to fall back, retry, or surface the error.
    """
    sys_blocks = [SystemPromptSection(text=system, cache_control=False)]
    msgs = [UserMessage(content=user)]

    text_parts: list[str] = []
    err: str | None = None
    aborted = False

    async def _drain() -> None:
        nonlocal err, aborted
        async for ev in provider.stream(
            model=model,
            system=sys_blocks,
            messages=msgs,
            tools=[],
            max_tokens=max_tokens,
            thinking="off",
        ):
            if isinstance(ev, TextDeltaEvent):
                text_parts.append(ev.delta)
            elif isinstance(ev, StreamErrorEvent):
                err = ev.error
                return
            elif isinstance(ev, DoneEvent):
                # Last-chance: gather text blocks from the assembled message in
                # case the SDK didn't emit incremental TextDeltaEvent.
                if not text_parts and isinstance(ev.message, AssistantMessage):
                    for blk in ev.message.content:
                        if isinstance(blk, TextContent):
                            text_parts.append(blk.text)
                return

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        aborted = True
        err = err or f"timeout after {timeout_seconds}s"
    except Exception as e:
        err = err or f"{type(e).__name__}: {e}"

    return LLMCallResult(text="".join(text_parts).strip(), aborted=aborted, error=err)
