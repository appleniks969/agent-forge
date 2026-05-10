# Agent Instructions — agent_forge

> **Who this file is for.** `AGENTS.md` is the architectural reference for
> contributors and AI coding assistants (Claude Code, Cursor, etc.) **modifying
> this codebase**. It complements — does not replace — the user docs.
>
> | Question | File |
> |---|---|
> | "How do I install and run agent-forge?" | **[docs/user/getting-started.md](docs/user/getting-started.md)** |
> | "How do I configure auth, models, thinking modes?" | **[docs/user/configuration.md](docs/user/configuration.md)** |
> | "What does this slash command do? / I hit an error" | **[docs/user/faq.md](docs/user/faq.md)** |
> | "How do I use the wiki or autonomous mode?" | **[README.md](README.md)** |
> | "If I change X, what else breaks?" | **AGENTS.md** (this file) |
> | "Where is symbol Y defined?" | **AGENTS.md** → Concept Index |
> | "What conventions does the code follow?" | **AGENTS.md** → Python Conventions |
> | "What invariants must I preserve?" | **AGENTS.md** → Policies |
>
> The user docs and AGENTS.md have ~no content overlap by design. CLI flags
> and slash commands live in `docs/user/` (single source of truth).

A minimal Python coding agent: 19 flat modules + one optional subpackage
(`wiki/` — see [Wiki Subsystem](#wiki-subsystem)), async-generator loop,
interactive REPL (`agent-forge`) and autonomous git-isolated pipeline
(`AutonomousFlow`).

---

## Module Dependency Order

Leaf → root. Lower modules must never import from higher ones.

```
messages.py            ← leaf: shared value types (Messages, TokenUsage, ToolResult, …)
models.py              ← leaf: Model catalog + ModelCost
provider.py            ← messages · models  (LLMProvider Protocol + StreamEvent union)
anthropic_provider.py  ← messages · models · provider  (only file that imports the SDK)
events.py              ← messages  (16 AgentEvent dataclasses + ToolCallRecord + AgentEvent union)
hooks.py               ← messages  (Hooks Protocol + NoopHooks + HookDecision)
_subprocess.py         ← leaf: asyncio subprocess wrapper (signal/abort-aware)
tools.py               ← messages · _subprocess
context.py             ← messages · models
system_prompt.py       ← messages
session.py             ← messages
loop.py                ← messages · models · provider · tools · events · hooks
prompts.py             ← messages · context · system_prompt · session · tools
runner.py              ← messages · loop  (drive() — the single drain seam)
runtime.py             ← messages · models · provider · context · system_prompt · tools · loop · runner
renderer.py            ← messages · loop
chat.py                ← all modules (composition root — REPL)
autonomous.py          ← messages · models · loop · prompts · renderer · runner · runtime · tools · _subprocess
                                          (composition root — autonomous pipeline)
```

| Module | Owns | Must NOT contain |
|---|---|---|
| `messages.py` | `UserMessage` / `AssistantMessage` / `ToolResultMessage`, content blocks, `TokenUsage` / `ZERO_USAGE`, `ToolResult`, `ToolDefinition`, `SystemPromptSection` | Any internal import |
| `models.py` | `Model`, `ModelCost`, `MODELS` dict, `DEFAULT_MODEL` | Any internal import |
| `provider.py` | `LLMProvider` Protocol, 7 `StreamEvent` dataclasses (block-lifecycle) | The Anthropic SDK; concrete adapter logic; `AnthropicProvider` re-export |
| `anthropic_provider.py` | `AnthropicProvider`, OAuth/API-key dispatch, system-as-user injection, JSON repair, `_to_api_messages()` | Anything outside the Anthropic wire format |
| `events.py` | 16 `*AgentEvent` dataclasses + `AgentEvent` union + `ToolCallRecord` | Loop algorithm, hooks, provider plumbing |
| `hooks.py` | `Hooks` Protocol, `NoopHooks`, `HookDecision`, `_hook_*` helpers | Loop algorithm, concrete hook subclasses |
| `_subprocess.py` | `run()` async subprocess wrapper that races against an abort `asyncio.Event` and times out | Tool-specific logic; never raises for normal exits |
| `tools.py` | `Tool` Protocol, `ToolRegistry`, 6 built-in tools (Bash/Read/Write/Edit/Grep/Find), `_sandbox()`, `_cap()` | LLM calls, session state, context logic, sync subprocess |
| `context.py` | `ContextWindow` aggregate, `PressureTier`, P4 eviction, token estimation, `CompactionPort`, `ContextBudget` (with `p4_max_bytes` / `tool_max_bytes`) | File I/O, LLM calls, session persistence, system-prompt assembly |
| `system_prompt.py` | `SystemPrompt` aggregate, `SectionName` (StrEnum w/ `.order` + `.cache_group`), cache-placement policy | File I/O, repo-map building, AGENTS.md loading |
| `session.py` | JSONL append log, session resume, memory.md read/write, `index.json` cwd→session lookup | LLM calls, context window logic, tool execution |
| `loop.py` | `agent_loop()` async generator, `AgentConfig` / `AgentResult`, retry policy, tool-result truncation | Session persistence, UI/ANSI, memory I/O, `_CwdPatchedRegistry` (gone — `AgentConfig.cwd` is passed directly to `tool.execute`) |
| `prompts.py` | `build_chat_prompt()`, `build_autonomous_prompt()`, public composables (`tools_section`, `discover_skills`, `load_agents_doc`, `build_repo_map`, `environment_section`) | ANSI output, event handling, agent loop |
| `runner.py` | `drive()` — drain `agent_loop` into an `AgentResult`, single seam | Session persistence, KeyboardInterrupt handling |
| `runtime.py` | `AgentRuntime` — pairs a `ContextWindow` with `make_config()` + `runner.drive()`. One `run_turn()` per user message, runs `manage_pressure()` automatically | Session JSONL persistence, REPL input, worktree lifecycle |
| `renderer.py` | ANSI helpers, `render_event()`, markdown printer, `print_footer()`, Rich console | Business logic, file I/O, agent-loop control |
| `chat.py` | Interactive REPL, paste handling, slash commands (`/quit /clear /status /model /remember`), `main` CLI entry | Anthropic wire format, tool implementations, direct `agent_loop` iteration (use `AgentRuntime.run_turn()`) |
| `autonomous.py` | `AutonomousFlow` state machine, git worktree lifecycle, `BashGuardHook`, `PathGuardHook`, `_CompositeHook`, delivery | Session JSONL persistence, REPL input, direct `agent_loop` iteration |

---

## Concept Index

Jump directly to the symbol — grep it in the file rather than scanning.

### Messages & token economics

| Concept | File → Symbol |
|---|---|
| all message types | `messages.py` → `UserMessage`, `AssistantMessage`, `ToolResultMessage`, `Message` union |
| content block types | `messages.py` → `TextContent`, `ThinkingContent`, `ToolCallContent`, `ImageContent`, `ContentBlock` union |
| vision input on user message | `messages.py` → `UserMessage.content: str \| tuple[TextContent \| ImageContent, ...]` |
| token usage | `messages.py` → `TokenUsage`, `ZERO_USAGE` |
| tool plumbing | `messages.py` → `ToolResult`, `ToolDefinition` |
| system prompt section type | `messages.py` → `SystemPromptSection` (with `.hint_cache` advisory alias) |

### Models

| Concept | File → Symbol |
|---|---|
| model catalog / pricing | `models.py` → `MODELS`, `DEFAULT_MODEL`, `ModelCost` |
| model lookup | `models.py` → `Model.from_id()` |

### Provider seam

| Concept | File → Symbol |
|---|---|
| LLM Provider Protocol | `provider.py` → `LLMProvider` |
| stream event union (7 events) | `provider.py` → `StreamEvent`, `ContentBlockStartEvent`, `TextDeltaEvent`, `ThinkingDeltaEvent`, `ToolCallEndEvent`, `ContentBlockEndEvent`, `DoneEvent`, `StreamErrorEvent` |
| Anthropic streaming adapter | `anthropic_provider.py` → `AnthropicProvider.stream()`, `_do_stream()` |
| OAuth vs API key dispatch | `anthropic_provider.py` → `_is_oauth()`, `AnthropicProvider.__init__` |
| message → Anthropic API format | `anthropic_provider.py` → `_to_api_messages()`, `_tool_result_block()` |
| JSON repair (truncated tool args) | `anthropic_provider.py` → `_repair_json()` |
| adaptive thinking model gate | `anthropic_provider.py` → `_supports_adaptive_thinking()` |
| system-as-user injection (OAuth) | `anthropic_provider.py` → `_system_already_injected()` |

### Async subprocess utility

| Concept | File → Symbol |
|---|---|
| signal-aware subprocess runner | `_subprocess.py` → `run()` (returns `(returncode, stdout, stderr, aborted)`) |

### Tools

| Concept | File → Symbol |
|---|---|
| tool protocol | `tools.py` → `Tool` (Protocol) |
| tool registry | `tools.py` → `ToolRegistry`, `default_registry()` |
| path sandboxing | `tools.py` → `_sandbox()` |
| tool output cap (50 KB) | `tools.py` → `_MAX_OUTPUT`, `_cap()` |
| 6 tool implementations | `tools.py` → `BashTool` · `ReadTool` · `WriteTool` · `EditTool` · `GrepTool` · `FindTool` |
| edit overlap detection | `tools.py` → `EditTool.execute()` (Phase 1.5 in the two-phase commit) |

### Context window

| Concept | File → Symbol |
|---|---|
| context window aggregate | `context.py` → `ContextWindow` |
| context budget config | `context.py` → `ContextBudget` (`p4_max_bytes` / `tool_max_bytes` configurable), `default_budget()` |
| context pressure tiers | `context.py` → `PressureTier`, `assess_pressure()` |
| pressure absolute thresholds | `context.py` → `ABSOLUTE_P4`, `ABSOLUTE_P3`, `ABSOLUTE_AGG` |
| P4 eviction (truncate old tool results) | `context.py` → `evict_p4()`, `_P4_NOTICE` |
| token estimation | `context.py` → `estimate_tokens()`, `estimate_tokens_list()` |
| action log eviction | `context.py` → `ContextWindow.receive()`, `ActionLogEntry`, `StratifiedWindowStrategy.summarise_turn()` |
| build LLM message array | `context.py` → `ContextWindow.build_messages()`, `StratifiedWindowStrategy.build()` |
| compaction port (DI boundary) | `context.py` → `CompactionPort`, `CompactionResult` |
| manage pressure (one-call facade) | `context.py` → `ContextWindow.manage_pressure()` |
| session resume from JSONL | `context.py` → `ContextWindow.init_from_existing()` |

### System prompt

| Concept | File → Symbol |
|---|---|
| ordered system prompt sections | `system_prompt.py` → `SystemPrompt`, `SectionName` |
| section ordering | `system_prompt.py` → `SectionName.order` property |
| cache group assignment | `system_prompt.py` → `SectionName.cache_group` property |
| volatile section flag | `system_prompt.py` → `SectionName.is_volatile` property |
| cache placement (last per group) | `system_prompt.py` → `SystemPrompt.build()` |
| plugin extras (uncached) | `system_prompt.py` → `SystemPrompt.register_extra()` |
| invalidation on /clear | `system_prompt.py` → `SystemPrompt.invalidate_session()`, `invalidate_all()` |

### Session & memory

| Concept | File → Symbol |
|---|---|
| session JSONL write | `session.py` → `append_message()`, `append_metadata()`, `append_compaction()` |
| session directory path | `session.py` → `sessions_dir()` |
| session resume / deserialise | `session.py` → `resume_session()`, `_dict_to_msg()` (re-stitches outer-entry usage onto `AssistantMessage.usage`) |
| latest session for cwd (O(1) index) | `session.py` → `latest_session_id()`, `_read_index()` / `_write_index()` (`~/.agent-forge/sessions/index.json`) |
| memory load (merged global+project) | `session.py` → `load_memory()`, `load_memory_deduped()` |
| memory update / dedup / cap | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS`, `_DEDUP_PREFIX` |

### Agent events & hooks

| Concept | File → Symbol |
|---|---|
| 16 agent event dataclasses | `events.py` → `AgentEvent` union (turn / thinking / text / tool / error / abort / compaction / done) |
| tool-call record (action log entry) | `events.py` → `ToolCallRecord` |
| hooks protocol | `hooks.py` → `Hooks` (Protocol), `NoopHooks`, `HookDecision` |
| hook call helpers | `hooks.py` → `_hook_before_llm()`, `_hook_before_tool()`, `_hook_after_tool()` |

### Agent loop

| Concept | File → Symbol |
|---|---|
| agent loop entry point | `loop.py` → `agent_loop()` |
| agent convenience drain | `loop.py` → `run_agent()` |
| agent config factory | `loop.py` → `make_config()` (lazily imports `AnthropicProvider`; takes `cwd=`) |
| agent config / result | `loop.py` → `AgentConfig` (carries `cwd` + `tool_max_bytes`), `AgentResult` |
| retry count / delay / jitter | `loop.py` → `_MAX_RETRIES`, `_BASE_DELAY`, `_MAX_DELAY`, `_retry_delay()` |
| tool result size cap (configurable) | `loop.py` → `_MAX_TOOL_BYTES`, `_truncate_tool_result()` (override via `AgentConfig.tool_max_bytes`) |
| cwd injection into tools | `loop.py` → `AgentConfig.cwd` passed straight to `tool.execute(args, cwd=…)` (no proxy registry) |

### Runner & runtime seams

| Concept | File → Symbol |
|---|---|
| drain agent_loop → AgentResult | `runner.py` → `drive()` |
| per-session glue (ctx + cfg factory + drive) | `runtime.py` → `AgentRuntime` (`run_turn()`, `clear()`, `init_messages()`) |

### Prompts (composition)

| Concept | File → Symbol |
|---|---|
| REPL system-prompt builder | `prompts.py` → `build_chat_prompt()` (alias `build_system_prompt`) |
| autonomous phase prompt builder | `prompts.py` → `build_autonomous_prompt()` |
| stable tools text section | `prompts.py` → `tools_section()` |
| skills discovery | `prompts.py` → `discover_skills()` |
| AGENTS.md / CLAUDE.md loader | `prompts.py` → `load_agents_doc()` |
| repo map builder | `prompts.py` → `build_repo_map()` |
| environment section (cwd/branch/date) | `prompts.py` → `environment_section()` |
| identity / guidelines text constants | `prompts.py` → `CHAT_IDENTITY`, `CHAT_GUIDELINES`, `PLAN_*`, `EXECUTE_*`, `VERIFY_*` |

### Renderer

| Concept | File → Symbol |
|---|---|
| event renderer (ANSI) | `renderer.py` → `render_event()` |
| ANSI colour helpers | `renderer.py` → `dim()` · `bold()` · `green()` · `red()` · `yellow()` · `cyan()` |
| turn footer printer | `renderer.py` → `print_footer()` |
| Rich console singleton | `renderer.py` → `get_console()` |

### Chat (REPL)

| Concept | File → Symbol |
|---|---|
| interactive REPL | `chat.py` → `run_chat()` (uses `AgentRuntime.run_turn()`) |
| CLI entry point | `chat.py` → `main()` |
| paste collapse / expand | `chat.py` → `_make_paste_bindings()`, `_expand_pastes()` |
| single-prompt (non-interactive) | `chat.py` → `_run_single_prompt()` |
| explicit memory save | `chat.py` → `/remember <text>` slash command (heuristic learning extractor was deleted in Phase 6) |

### Autonomous

| Concept | File → Symbol |
|---|---|
| autonomous state machine | `autonomous.py` → `AutonomousFlow`, `FlowState` |
| destructive-bash guard hook | `autonomous.py` → `BashGuardHook` (subclass of `NoopHooks`) |
| sensitive-path guard hook | `autonomous.py` → `PathGuardHook` (subclass of `NoopHooks`) |
| compose multiple hooks | `autonomous.py` → `_CompositeHook` |
| gate checks (clean tree, not detached) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| git worktree create / cleanup | `autonomous.py` → `_create_worktree()`, `_cleanup_worktree()` |
| phase-runtime factory | `autonomous.py` → `AutonomousFlow._phase_runtime()` (one `AgentRuntime` per phase) |
| plan / execute / verify phases | `autonomous.py` → `_plan()`, `_execute()`, `_verify_agent()` |
| verify commands runner | `autonomous.py` → `AutonomousFlow._verify()` |
| delivery (pr / merge / output / none) | `autonomous.py` → `AutonomousFlow._deliver()` |
| autonomous entry point | `autonomous.py` → `run_autonomous()` |

---

## Change Impact Map

When you change a type or interface, also update these downstream files.

| Changed | Also update |
|---|---|
| `messages.py` → any `Message` type | `context.py` · `session.py` · `loop.py` · `anthropic_provider.py` · `renderer.py` · `chat.py` · `autonomous.py` |
| `messages.py` → `UserMessage.content` (vision) | `session.py` (`_msg_to_dict` / `_dict_to_msg`) · `anthropic_provider.py` (`_to_api_messages`) · `context.py` (`estimate_tokens`) · `tests/test_session_roundtrip.py` |
| `messages.py` → `SystemPromptSection` | `system_prompt.py` (`SystemPrompt.build()` returns it) · `loop.py` (`AgentConfig` carries it) · `anthropic_provider.py` (consumes it in `stream()`) · `prompts.py` |
| `messages.py` → `TokenUsage` fields | `loop.py` (`AgentResult`) · `session.py` (`append_message` + `resume_session` re-stitch) · `renderer.py` (`print_footer`) · `chat.py` · `anthropic_provider.py` (`_extract_usage`) |
| `messages.py` → `ToolDefinition` | `tools.py` (`Tool.definition()`) · `loop.py` (tool_defs in `_stream_one_turn`) · `provider.py` (`LLMProvider.stream` signature) |
| `messages.py` → `ToolResult` | `tools.py` (every tool's `__call__` return) · `loop.py` (`_truncate_tool_result`) · `autonomous.py` (`BashGuardHook` synthesises one) |
| `models.py` → `Model` | `context.py` (`assess_pressure`, `default_budget`) · `loop.py` (`AgentConfig`) · `anthropic_provider.py` (`stream` signature) · `chat.py` · `autonomous.py` · `runtime.py` |
| `models.py` → `MODELS` | `chat.py` (/model slash command display) |
| `provider.py` → `LLMProvider` Protocol | `anthropic_provider.py` (must satisfy it) · `loop.py` (`_stream_one_turn` consumes it) · `runtime.py` (carries it) · `tests/fake_provider.py` |
| `provider.py` → any `StreamEvent` type | `anthropic_provider.py` (yields them) · `loop.py` (`_stream_one_turn` switch) |
| `events.py` → any `*AgentEvent` | `renderer.py` (`render_event` handles every branch) · `chat.py` · `autonomous.py` · `tests/fake_provider.py` |
| `hooks.py` → `Hooks` Protocol | `NoopHooks` (same file) · `BashGuardHook` / `PathGuardHook` / `_CompositeHook` (`autonomous.py`) · any new hook subclass · `tests/test_hooks.py` |
| `_subprocess.py` → `run()` signature | `tools.py` (`BashTool`, `GrepTool` rg fallback) · `autonomous.py` (gate / worktree / verify / deliver) |
| `context.py` → `ContextWindow` interface | `runtime.py` (sole user inside the package) · `chat.py` / `autonomous.py` only via `runtime.context.*` getters |
| `context.py` → `ContextBudget` fields | `default_budget()` (same file) · `evict_p4()` consumers if a new threshold is added |
| `system_prompt.py` → `SystemPrompt` / `SectionName` | `prompts.py` (`build_chat_prompt`, `build_autonomous_prompt`) · `runtime.py` (carries it) · `chat.py` / `autonomous.py` (pass through to runtime) |
| `session.py` → JSONL entry format | `append_message()` + `_msg_to_dict()` (write side) · `resume_session()` + `_dict_to_msg()` (read side) — both sides must stay in sync · `tests/test_session_roundtrip.py` |
| `session.py` → `index.json` schema | `_read_index()` / `_write_index()` / `latest_session_id()` (rebuild path) · `tests/test_phase6.py` |
| `loop.py` → `AgentConfig` fields | `make_config()` (same file) · `runtime.py` (`AgentRuntime.make_cfg()`) · tests using `make_config(...)` directly |
| `loop.py` → `AgentResult` fields | `chat.py` (result handling, session persistence) · `autonomous.py` (`_execute` returns it) · `runner.py` / `runtime.py` (transparent) |
| `runner.py` → `drive()` signature | `runtime.py` (`AgentRuntime.run_turn()`) · `tests/test_runner.py` |
| `runtime.py` → `AgentRuntime` API | `chat.py` (REPL: `run_turn`, `clear`, `init_messages`, `context`) · `autonomous.py` (`_phase_runtime` + `run_turn` calls) |
| `tools.py` → `Tool` protocol | All 6 tool classes in same file · `loop.py` calls `tool.execute(args, cwd=…, signal=…)` directly |
| `tools.py` → `ToolRegistry` interface | `loop.py` (`config.tool_registry.get / definitions`) · `runtime.py` · `chat.py` · `autonomous.py` · `prompts.py` (`tools_section`) |
| `prompts.py` → `build_chat_prompt` signature | `chat.py` (sole caller) |
| `prompts.py` → `build_autonomous_prompt` signature | `autonomous.py` (`_phase_runtime`) |
| Add a new `SectionName` | `system_prompt.py` (enum variant + update `.order` + `.cache_group`) · `prompts.py` (`register()` call in the relevant builder) |
| Add a new tool | `tools.py` (`default_registry()`) · `prompts.py` (`tools_section` description) |
| Add a new provider adapter | New file `<vendor>_provider.py` satisfying `LLMProvider` · register/select in `chat.py` · `make_config(provider=…)` for tests |

---

## Common Extension Recipes

| Task | Steps |
|---|---|
| Add a new tool | Implement class in `tools.py` following `BashTool` pattern (use `_subprocess.run` for shell-out; never `subprocess.run`) · add to `default_registry()` · add one-line description to `tools_section()` in `prompts.py` |
| Add a new model | Add entry to `MODELS` dict in `models.py` · update `DEFAULT_MODEL` if needed |
| Add a new system-prompt section | Add `SectionName` variant in `system_prompt.py` (set `.order` + `.cache_group` properties) · call `sp.register(SectionName.X, …)` in `prompts.py:build_chat_prompt()` and/or `build_autonomous_prompt()` |
| Add a new AgentEvent | Add frozen dataclass in `events.py` · add to `AgentEvent` union · handle the new `isinstance` branch in `renderer.py:render_event()` |
| Change compaction logic | Implement a `CompactionPort` subclass · inject via `ContextWindow(compaction_port=…)` · `ContextWindow.manage_pressure()` will call it at P3/AGG |
| Add a Hook (e.g. policy / guard) | Subclass `NoopHooks` in any composition root · override `before_llm` / `before_tool_call` / `after_tool_call` · pass via `make_config(hooks=…)` (or `_CompositeHook(...)` to chain) |
| Add a new provider | Implement a class satisfying the `LLMProvider` Protocol in a sibling file · keep all SDK imports inside it · pass via `make_config(provider=…)` (no global state) · or pass via `AgentRuntime(provider=…)` |
| Inject a non-cached prompt section from a plugin | Call `sp.register_extra("name", lambda: text)` — appended after named sections, never cached |
| Tighten the tool-result truncation cap | Pass `tool_max_bytes` via `make_config(...)` or set `AgentConfig.tool_max_bytes` directly (default 50 KB) |
| Tighten the P4 eviction threshold | Construct a custom `ContextBudget(p4_max_bytes=…)` and pass to `ContextWindow(budget=…)` |

---

## Python Conventions

- Python ≥ 3.12; use `X | Y` union syntax (not `Optional` / `Union`)
- `from __future__ import annotations` at the top of every file
- All public value objects: `@dataclass(frozen=True)`
- All tools: never raise — always return `ToolResult(is_error=True)` on failure
- All shell-outs go through `_subprocess.run()` — never `subprocess.run` (blocks the event loop, ignores aborts)
- Async all the way: tool execution, provider streaming, agent loop, REPL are all `async`
- Section headers in files: `# ── SectionName ──────────────────────────────────────────────`
- Module docstring in every file: purpose + dependency position (why it exists relative to its neighbours)

---

## Verification

Run these after every change.

```bash
# Install / reinstall the package in editable mode (uv)
uv pip install -e .

# Run the test suite (353 tests as of MVP-2 wiki + folder-per-stage refactor)
uv run pytest -q

# Check imports
python -c "import agent_forge; print('ok')"

# Smoke-test the CLI
agent-forge --help
```

---

## CLI Flags & Slash Commands

User-facing reference lives in **[docs/user/configuration.md](docs/user/configuration.md)** (CLI flags) and **[docs/user/faq.md](docs/user/faq.md)** (slash commands). This file does not duplicate them — see the user docs to keep one source of truth.

Internal contract for contributors:

- `argparse` choices for `--thinking` are defined in `chat.py:_parse_args()`.
  When you add a new level, also update `docs/user/configuration.md` (CLI
  flags reference + Thinking modes table).
- Slash commands are dispatched in `chat.py:run_chat()`. New slash commands
  must (a) appear in the slash-command table in `docs/user/faq.md` and
  (b) update `_status_text()` if they affect session state.

Autonomous mode is invoked programmatically via `run_autonomous(AutonomousConfig(...))` — no CLI flag yet. See [README.md](README.md) for the Python API.

---

## Policies

| Policy | Location |
|---|---|
| Turn completeness: partial assistant messages never appended on error/abort | `loop.py` → `_stream_one_turn()` — only appends `assistant_msg` on `DoneEvent` |
| Abort completeness: remaining unexecuted tool calls get placeholder error results before `AbortedAgentEvent` | `loop.py` → tool execution loop, `tool_calls[i + 1:]` fill |
| Max-turns exits via `DoneAgentEvent(result.aborted=True)` — single exit path for callers | `loop.py` → end of `agent_loop()` while loop |
| Composition roots use `AgentRuntime.run_turn()` to drive a turn — never iterate `agent_loop` directly (only `runner.drive()` does) | `chat.py` (REPL + `_run_single_prompt`) · `autonomous.py` (`_plan` / `_execute` / `_verify_agent`) |
| Tool result truncation default 50 KB (loop-time, before context append; configurable per `AgentConfig.tool_max_bytes`) | `loop.py` → `_truncate_tool_result()`, `_MAX_TOOL_BYTES` |
| Tool output cap at 50 KB (tool-time, before returning) | `tools.py` → `_cap()`, `_MAX_OUTPUT` |
| Path sandboxing (cwd enforcement, reject `../` escapes) | `tools.py` → `_sandbox()` |
| Edit overlap detection (identical or nested old_strings rejected) | `tools.py` → `EditTool.execute()` (Phase 1.5 of two-phase commit) |
| All shell-outs are async + signal-aware (kill on abort, kill on timeout) | `_subprocess.py` → `run()` |
| Retry: exponential backoff + jitter, max 3 attempts, max 30 s — owned by loop, NOT provider | `loop.py` → `_retry_delay()`, `_MAX_RETRIES`, `_MAX_DELAY` |
| Hooks default to `NoopHooks` — composition roots opt in by passing `make_config(hooks=…)` or `AgentRuntime(hooks=…)` | `loop.py` → `AgentConfig.hooks` default · `autonomous.py` → `_CompositeHook(BashGuardHook(), PathGuardHook())` |
| Context pressure eviction (P4 inplace, P3/AGG compact) — runs after every turn via `AgentRuntime.run_turn()` | `runtime.py` → `run_turn()` calls `ctx.manage_pressure()` · `context.py` → `ContextWindow.manage_pressure()` |
| ActionLog: evicted turns become one-liner summaries, never discarded | `context.py` → `ContextWindow.receive()` · `StratifiedWindowStrategy.summarise_turn()` |
| CompactionPort is optional — P3/AGG fall back to P4 if absent | `context.py` → `ContextWindow.manage_pressure()` |
| Cache placement: `cache_control=True` on last non-null section of each group (advisory hint; providers may ignore) | `system_prompt.py` → `SystemPrompt.build()` · `messages.py` → `SystemPromptSection.hint_cache` alias |
| Volatile sections (ENVIRONMENT, CUSTOM, plugin extras) never cached | `system_prompt.py` → `SectionName.is_volatile`, group 3 + `register_extra` |
| AGENTS.md → CLAUDE.md fallback, 32 KB cap, truncation notice | `prompts.py` → `load_agents_doc()` |
| Memory deduplication (60-char prefix match) | `session.py` → `load_memory_deduped()`, `_DEDUP_PREFIX` |
| Memory size cap (~2 K tokens) | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS` |
| Session resume re-stitches outer-entry usage onto `AssistantMessage.usage` | `session.py` → `resume_session()` |
| `latest_session_id()` is O(1) via `~/.agent-forge/sessions/index.json`, with O(n) scan fallback | `session.py` → `_read_index()`, `_write_index()`, `latest_session_id()` |
| Gate checks before worktree creation (clean tree, named branch) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| Worktree cleanup on success, failure, or crash | `autonomous.py` → `AutonomousFlow.run()` try/finally |
| Delivery only if all verify commands pass | `autonomous.py` → `FlowState` machine: VERIFYING before DELIVERING |
| Destructive Bash blocked in autonomous mode | `autonomous.py` → `BashGuardHook.before_tool_call()` |
| Sensitive-path writes blocked in autonomous mode | `autonomous.py` → `PathGuardHook.before_tool_call()` |
| OAuth vs API key: different client, beta headers, system-as-user injection | `anthropic_provider.py` → `_is_oauth()` · `AnthropicProvider.stream()` · `_system_already_injected()` |
| `import agent_forge` works without the Anthropic SDK installed (best-effort import) | `__init__.py` → `try: from .anthropic_provider import AnthropicProvider` |

---

## Wiki Subsystem

Optional, **composition-only** subpackage at `agent_forge/wiki/`. None of the
17 core flat modules import from it. The two consumers are both composition
roots and both **lazy-import** (so `import agent_forge` works with `wiki/`
broken or absent):

- `prompts.py` → `wiki.present.build_wiki_section()` (per-turn, system-prompt seam)
- `chat.py` → `wiki.metrics.record_override` (`/wrong`), `wiki.metrics.summarise`
  (`/wiki`), `wiki.ratchet.ratchet_session` (`/ratchet`, auto on `/quit`
  when `--ratchet`), `wiki.gather.cli._main` (`agent-forge wiki ...`)

### Folder shape (uniform across all 7 stages)

Every stage is its own folder containing `__init__.py` (re-exports the public
surface) + `runner.py` (± `bundle.py`, `cli.py`, `discovery.py`, `builtin/`).
This is enforced — promoting one stage to a folder and leaving others flat
is a smell; pick the convention and stay consistent. See the `present/`,
`maintain/`, `metrics/` folders for the minimal-shape template.

```
agent_forge/wiki/
├── __init__.py             re-exports the wiki public surface
├── _llm.py                 shared: stream-and-collect helper around any LLMProvider
├── types.py                shared: Artifact, Source, Gatherer, GatherResult
├── storage.py              shared: all .agent-forge/ path + read/write helpers
│
├── gather/                 stage 1 — pull repo signal into raw/ (no LLM)
│   ├── cli.py               argparse for `agent-forge wiki <verb>` (mounts ALL verbs)
│   ├── discovery.py         entry-point + builtin gatherer discovery
│   └── builtin/             notes / repo_files / code_markers / git_history / prs / hotspots
├── compile/                stage 2 — raw/ → curated/ via LLM
│   ├── runner.py            compile_wiki(), DEFAULT_SKILL, _GLOBAL_OUTPUTS
│   └── bundle.py            build_compile_bundle()
├── present/                stage 3 — raw/curated/ → system-prompt section (no LLM)
│   └── runner.py            build_wiki_section()
├── ratchet/                stage 4 — session JSONL → raw/notes/session/ via LLM
│   ├── runner.py            ratchet_session(), DEFAULT_SKILL, load_skill
│   └── bundle.py            build_session_bundle()
├── compact/                stage 5 — lint curated/ via LLM (anti-rot)
│   └── runner.py            compact_wiki()
├── maintain/               stage 6 — detect stale areas, re-gather (no LLM)
│   └── runner.py            detect_stale_areas(), run_maintain(), MaintainResult
└── metrics/                stage 7 — citation / override / staleness logs
    └── runner.py            record_citation, record_override, snapshot_staleness, summarise
```

### Internal dependency rules

- `types.py`, `storage.py`, `_llm.py` are **shared leaves** — every stage may import them
- Stages **must not** import from each other except: `maintain/runner.py` may
  call `gather.run_gather` (rationale: maintain *is* gather-with-area-filter)
- LLM-using stages (`compile`, `ratchet`, `compact`) all use `_llm.run_llm()`
  + a `DEFAULT_SKILL` constant + `load_skill()` for per-repo override at
  `.agent-forge/skills/<stage>.md` — keep the convention when adding new
  LLM stages
- `gather/cli.py` is the single argparse mount point for **all** wiki verbs
  (gather, status, compile, ratchet, compact, maintain) so users get one
  uniform `agent-forge wiki ...` CLI; new verbs get added there, not in
  per-stage CLI files

### Change-impact (wiki internals)

| Changed | Also update |
|---|---|
| `types.py` → `Artifact` / `Source` | `storage.py` (serialisers) · every gatherer in `gather/builtin/` · `compile/bundle.py` · `present/runner.py` · `tests/wiki/test_types.py` |
| `storage.py` → directory layout | every stage `runner.py` (paths) · README "Layout under `.agent-forge/`" · `tests/wiki/test_storage.py` |
| Add a new gatherer | new module under `gather/builtin/` · register in `gather/discovery.py` · add to `--only` help text in `gather/cli.py` · add `tests/wiki/test_builtin_<name>.py` |
| Add a new wiki CLI verb | add `_handle_<verb>` + parser in `gather/cli.py:_add_action_subparsers()` · README "Wiki subcommand" · README "The seven stages" table |
| Change the conventions skeleton (`_markdown_skeleton`) | `present/runner.py` (algorithm) · `tests/wiki/test_present.py:test_repo_file_skeleton_keeps_all_section_headers` (contract) · README "The seven stages" note about skeleton extraction |
| Change `wiki init` area detection | `gather/cli.py:_detect_areas` · `_NEVER_AREAS` blacklist · `tests/wiki/test_cli.py` (`test_init_*` cases for packages/src/top-level) |
| Add a new wiki slash command | add to `chat.py:run_chat()` dispatch · README "Slash Commands" table · `_status_text()` if it affects session state |
| Promote a stage from one file to multi-file | follow the `compile/`-style template (`__init__.py` re-exports from `runner.py` + `bundle.py`); never go back to a flat `<stage>.py` (asymmetry confuses readers) |

---

## Reference Documents

| Document | When to read it |
|---|---|
| `pyproject.toml` | Dependency versions, entry-point wiring, dev-tool config |
| `docs/CHANGELOG.md` | Per-phase change log (phases 0-7) |
| `tests/fake_provider.py` | Reference implementation of `LLMProvider` for tests — copy this pattern when wiring a new provider |
| `tests/test_runner.py` | Canonical examples of how to drive `agent_loop` from a test |
| `tests/test_hooks.py` | Canonical examples of writing a custom `Hooks` subclass |
| `tests/test_phase6.py` | Examples of `PathGuardHook`, `EditTool` overlap detection, session index tests |

---

## Worked Examples

Longer, runnable code samples for the most common extension recipes. The terse one-line steps live in [Common Extension Recipes](#common-extension-recipes); use these when you want a copy-paste starting point.

### Add a custom tool

1. Implement a class following the `BashTool` pattern in `tools.py`:

```python
class MyTool:
    name = "MyTool"
    description = "One-line description of what the tool does."
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input value"},
        },
        "required": ["input"],
    }

    def definition(self):
        from agent_forge import ToolDefinition
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal=None):
        from agent_forge import ToolResult
        value = args.get("input", "")
        return ToolResult(content=f"Result: {value}")
```

2. Register it:

```python
from agent_forge import default_registry
registry = default_registry()
registry.register(MyTool())
```

3. Wire it into the loop:

```python
from agent_forge import make_config, agent_loop, UserMessage

cfg = make_config(
    model=..., api_key=..., system_prompt=...,
    tool_registry=registry, cwd=".",
)
async for event in agent_loop(cfg, [UserMessage(content="use MyTool")]):
    ...
```

### Add a new model

Add an entry to `MODELS` in `provider.py`:

```python
MODELS["claude-new-model"] = Model(
    id="claude-new-model",
    context_window=200_000,
    max_tokens=64_000,
    reasoning=True,
    cost=ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75),
)
```

### Generate API reference docs

```bash
bash scripts/build_api_docs.sh
open docs/api/agent_forge.html        # macOS
xdg-open docs/api/agent_forge.html    # linux
```

The generator (`pdoc`) renders every module, class, and function with its docstring. The `docs/api/` output is gitignored — rebuild after any docstring change.
