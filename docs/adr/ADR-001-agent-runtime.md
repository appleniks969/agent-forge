# ADR-001 — AgentRuntime

**Status:** accepted (Phase 3)
**Date:** 2026-05-02
**Deciders:** architecture pass

## Context

The repository had two composition roots — `chat.py` (REPL) and `autonomous.py`
(non-interactive pipeline) — that both had to drive `agent_loop()` for one
turn. Each one independently:

1. Built a `ContextWindow` (chat only — autonomous skipped this entirely).
2. Built `initial_messages` via `ctx.build_messages(user_msg)`.
3. Built an `AgentConfig` via `make_config(...)`, rotating the abort signal.
4. Drained the loop via `runner.drive(...)`.
5. Synced the real API token total back into the window.
6. Called `ctx.receive(user_msg, assistant_messages, tool_calls)`.
7. Re-synced.
8. Called `ctx.manage_pressure()`.

Steps 5–8 only existed in `chat.py`. **`autonomous.py` had no context
pressure management at all** (RC2). A long autonomous run with many
large tool results would silently exceed the model's context window.

The duplication also meant that any change to the per-turn protocol (e.g.
adding a hook before `manage_pressure`, or changing the order of sync) had
to be made in two places.

## Decision

Introduce `agent_forge.runtime.AgentRuntime`, a small dataclass that owns:

- a `Model`, a `SystemPrompt`, a `ToolRegistry`, a cwd, and an optional
  `LLMProvider` / `Hooks` / `api_key`;
- a `ContextWindow` (constructed in `__post_init__`).

It exposes exactly three methods:

```python
def init_messages(self, messages: list[Message]) -> None      # session resume
def clear(self) -> None                                       # /clear
async def run_turn(self, user_message, *, signal=None,
                   on_event=None) -> AgentResult | None       # the per-turn dance
```

`run_turn()` performs steps 2–8 in one call.

Both composition roots now call `runtime.run_turn(user_msg, ...)`. Autonomous
constructs a fresh `AgentRuntime` per phase (plan / execute / verify) — each
phase has a distinct system prompt and `max_turns`, so each gets its own
isolated `ContextWindow`. As a side-effect, every autonomous phase now runs
context pressure management on every turn.

## Consequences

**Positive**
- RC2 is fixed: autonomous gets `manage_pressure()` for free.
- The per-turn protocol lives in one place. Adding a step (e.g. a metrics
  callback) is a one-file change.
- `chat.py` shrinks ~50 LOC; `autonomous.py` shrinks ~30 LOC.
- The "what is the cwd of this run?" question has one answer
  (`AgentRuntime.cwd`), which lets us delete `_CwdPatchedRegistry` (Phase 4).

**Negative**
- One more module (`runtime.py`, ~150 LOC).
- Tests that previously called `make_config()` directly still work — but new
  tests targeting per-turn behaviour should prefer constructing an
  `AgentRuntime`.

**Bounded**
- The runtime API is deliberately three methods. Anything richer
  (slash commands, persistence, KeyboardInterrupt handling, worktree
  lifecycle) belongs in the composition root, not here. If `AgentRuntime`
  ever grows a fourth public method, that should trigger a re-read of this
  ADR.
