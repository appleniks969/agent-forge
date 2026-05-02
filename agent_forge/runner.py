"""
runner.py — drive(): drain agent_loop into an AgentResult, no exceptions.

Depends on loop. The five drain loops in chat.py + autonomous.py (run_chat,
_run_single_prompt, _plan, _execute, _verify_agent) were structurally identical:

    result = None
    async for ev in agent_loop(cfg, msgs):
        if isinstance(ev, DoneAgentEvent):
            result = ev.result
        render_event(ev, verbose)

This module collapses that pattern. drive() is a single coroutine that always
returns an AgentResult — for aborted or fatally-errored runs it synthesises
an AgentResult(aborted=True) so callers have one exit shape.

Owns: drive() (composition-root drain helper).
Does NOT own: persistence (chat.py persists after drive() returns; autonomous
              doesn't persist), KeyboardInterrupt handling (REPL-specific —
              chat.py wraps the drive() call in try/except).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from .loop import (
    AgentConfig, AgentEvent, AgentResult, DoneAgentEvent, agent_loop,
)
from .messages import Message


async def drive(
    config: AgentConfig,
    initial_messages: list[Message],
    *,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentResult | None:
    """Drain agent_loop, returning the final AgentResult or None.

    Returns:
      AgentResult — agent_loop yielded DoneAgentEvent (clean finish OR max-turns;
                    distinguish via .aborted on the result).
      None        — agent_loop ended without DoneAgentEvent (signal abort or
                    fatal pre-Done error). Caller has nothing to persist or
                    summarise.

    on_event, if supplied, is called once per yielded AgentEvent before drive()
    inspects it. Sync-only — render_event is sync and that covers every caller
    in this codebase. (loop.run_agent supports async on_event for programmatic
    use; drive() stays sync to avoid the iscoroutinefunction branch in the
    hot path of every event.)
    """
    async for ev in agent_loop(config, initial_messages):
        if on_event is not None:
            on_event(ev)
        if isinstance(ev, DoneAgentEvent):
            return ev.result
    return None
