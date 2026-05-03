# Changelog

All notable architectural changes from the Phase 0–7 cleanup pass.

The version bump is non-breaking at runtime; the only mildly-breaking change
is that `AgentConfig.provider` is now typed as the `LLMProvider` Protocol
instead of the concrete `AnthropicProvider` class. This is type-checker-visible
but runtime-compatible (the concrete class still satisfies the Protocol).

## [Unreleased] — Phase 0-7 architecture pass

### Added
- **`agent_forge.runtime.AgentRuntime`** — pairs a `ContextWindow` with
  an `AgentConfig` factory and `runner.drive()`. One `run_turn()` per user
  message; runs `manage_pressure()` automatically. See ADR-001.
  Both composition roots (`chat.py`, `autonomous.py`) now call it.
- **`agent_forge.events`** — extracted from `loop.py`. Owns the 16
  `*AgentEvent` dataclasses + `AgentEvent` union + `ToolCallRecord`.
  Re-exported from `loop.py` for back-compat.
- **`agent_forge.hooks`** — extracted from `loop.py`. Owns the `Hooks`
  Protocol, `NoopHooks`, `HookDecision`, and `_hook_*` helpers.
  Re-exported from `loop.py` for back-compat.
- **`agent_forge._subprocess`** — async, signal-aware subprocess wrapper.
  Replaces every blocking `subprocess.run` in `tools.py` and `autonomous.py`.
  Mid-Bash `Ctrl-C` now actually kills the subprocess (RC3).
- **`PathGuardHook`** in `autonomous.py` — denies `Write` / `Edit` to a
  configurable list of sensitive paths (`/etc`, `~/.ssh`, `~/.aws`, `/usr`,
  `/bin`, `/sbin`, `/boot`, `/sys`, `/proc`, `~/.gnupg` by default).
- **`_CompositeHook`** in `autonomous.py` — chains multiple `Hooks`; first
  veto wins. Wired by `_execute()` as `_CompositeHook(BashGuardHook(),
  PathGuardHook())`.
- **`/remember <text>`** slash command — explicit memory save, replacing the
  deleted heuristic `_extract_learnings` / `_save_learnings`.
- **`session.index.json`** at `~/.agent-forge/sessions/index.json` — O(1)
  cwd→session lookup. `latest_session_id()` reads it; falls back to O(n)
  scan if missing or stale.
- **`AssistantMessage.usage` round-trip on resume** — `resume_session()`
  now re-stitches the outer JSONL entry's `usage` field onto the
  reconstructed `AssistantMessage`. Cost/token accounting is preserved
  across `--continue`.
- **Vision support on `UserMessage.content`** — type widened from
  `str | tuple[TextContent, ...]` to
  `str | tuple[TextContent | ImageContent, ...]`. Round-trips through
  `session._msg_to_dict` / `_dict_to_msg` and through
  `anthropic_provider._to_api_messages`.
- **`SystemPromptSection.hint_cache`** `@property` — forward-compatible alias
  for `cache_control`. See ADR-003.
- **`ContextBudget.p4_max_bytes` / `tool_max_bytes`** — configurable
  thresholds. Default values unchanged (1 KB / 50 KB respectively).
- **`AgentConfig.tool_max_bytes`** — overrides the default 50 KB tool-result
  truncation cap at loop time.
- **`EditTool` overlap detection** — Phase 1.5 of the two-phase commit
  rejects identical or nested `old_string`s rather than silently
  mis-replacing.
- ADRs under `docs/adr/`: `ADR-001-agent-runtime.md`,
  `ADR-002-provider-as-protocol.md`, `ADR-003-cache-control-is-a-hint.md`.
- New tests: `tests/test_phase6.py` (~14 tests covering the additions
  above). Existing test count rose from 179 → 197.

### Changed
- **`AgentConfig.provider` is now typed `LLMProvider`** (Protocol), not
  the concrete `AnthropicProvider`. See ADR-002. Runtime-compatible;
  type-checker-visible.
- **`AgentConfig.cwd`** is now read by the loop and passed straight to
  `tool.execute(args, cwd=…)`. The wrapping `_CwdPatchedRegistry` /
  `_CwdBoundTool` proxy classes were deleted (~50 LOC removed from
  `loop.py`).
- **`make_config()`** lazily imports `AnthropicProvider` only when no
  `provider=` is passed. `loop.py` no longer pulls in the SDK at module
  load.
- **`SystemPrompt.build()`** — collapsed the duplicate empty-resolved
  branch. Single code path now handles "no named sections + only extras"
  via the same loop.
- **`chat.py`**: REPL turn body now `await runtime.run_turn(user_msg, ...)`
  instead of inlining the build/drive/sync/receive/manage dance.
- **`autonomous.py`**: each phase (`_plan`, `_execute`, `_verify_agent`)
  goes through `_phase_runtime(phase, max_turns=…, hooks=…)` →
  `runtime.run_turn(...)`. Autonomous now gets context pressure management
  on every turn (RC2 fix).

### Removed
- **`agent_forge.loop._CwdPatchedRegistry`** and **`_CwdBoundTool`** —
  cwd injection is now `AgentConfig.cwd` → `tool.execute(args, cwd=…)`.
- **Re-export of `SystemPrompt` / `SectionName` from `agent_forge.context`**
  — they live only in `agent_forge.system_prompt` now (RC4 cleanup).
- **`agent_forge.chat._extract_learnings` / `_save_learnings`** — the
  60-line heuristic that scanned user messages for "don't / instead of /
  always / never" markers. Replaced by explicit `/remember <text>`.
- **Re-export of concrete `AnthropicProvider` from `agent_forge.provider`**
  (Phase 1; was already cleaned up).

### Fixed
- **RC1** — pure `pip install agent-forge` (no extras) now imports cleanly
  without the Anthropic SDK; `AnthropicProvider` is silently `None` if the
  SDK is missing.
- **RC2** — autonomous mode now runs context pressure management
  per-turn via `AgentRuntime`.
- **RC3** — `Ctrl-C` during `Bash` actually kills the subprocess; aborts
  propagate through `_subprocess.run()`.
- **RC4** — `loop.py` no longer carries the cwd-patching proxy classes;
  context.py no longer re-exports system-prompt symbols; `SystemPrompt.build()`
  has one code path.
- **RC5** — `cache_control` documented as advisory (ADR-003); `hint_cache`
  alias added.

### Deprecated
- `SystemPromptSection.cache_control` is now spelled `hint_cache` in new
  code. The old name keeps working for at least one release.

### Module count
- Before: 13 modules (`messages`, `models`, `provider`, `anthropic_provider`,
  `tools`, `context`, `system_prompt`, `session`, `loop`, `prompts`,
  `runner`, `renderer`, `chat`, `autonomous`).
- After: 17 modules (added `events`, `hooks`, `_subprocess`, `runtime`).

### Test count
- Before: 179 passing.
- After: 197 passing (1.5 s).
