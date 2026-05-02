# Agent Instructions — agent_forge

A minimal Python coding agent: 13 flat modules, async-generator loop, interactive REPL (`agent-forge`) and autonomous git-isolated pipeline (`AutonomousFlow`).

---

## Module Dependency Order

Leaf → root. Lower modules must never import from higher ones.

```
messages.py            ← leaf: shared value types (Messages, TokenUsage, ToolResult, …)
models.py              ← leaf: Model catalog + ModelCost
provider.py            ← messages · models  (LLMProvider Protocol + StreamEvent union)
anthropic_provider.py  ← messages · models · provider  (only file that imports the SDK)
tools.py               ← messages
context.py             ← messages · models
system_prompt.py       ← messages
session.py             ← messages
loop.py                ← messages · models · provider · tools · context
prompts.py             ← messages · context · system_prompt · session · tools
renderer.py            ← messages · loop
runner.py              ← messages · loop  (drive() — the single drain seam)
chat.py                ← all modules (composition root — REPL)
autonomous.py          ← messages · models · loop · prompts · renderer · runner · tools
                                          (composition root — autonomous pipeline)
```

| Module | Owns | Must NOT contain |
|---|---|---|
| `messages.py` | `UserMessage` / `AssistantMessage` / `ToolResultMessage`, content blocks, `TokenUsage` / `ZERO_USAGE`, `ToolResult`, `ToolDefinition`, `SystemPromptSection` | Any internal import |
| `models.py` | `Model`, `ModelCost`, `MODELS` dict, `DEFAULT_MODEL` | Any internal import |
| `provider.py` | `LLMProvider` Protocol, 7 `StreamEvent` dataclasses (block-lifecycle) | The Anthropic SDK; concrete adapter logic |
| `anthropic_provider.py` | `AnthropicProvider`, OAuth/API-key dispatch, system-as-user injection, JSON repair, `_to_api_messages()` | Anything outside the Anthropic wire format |
| `tools.py` | `Tool` Protocol, `ToolRegistry`, 6 built-in tools (Bash/Read/Write/Edit/Grep/Find), `_sandbox()`, `_cap()` | LLM calls, session state, context logic |
| `context.py` | `ContextWindow` aggregate, `PressureTier`, P4 eviction, token estimation, `CompactionPort` | File I/O, LLM calls, session persistence, system-prompt assembly |
| `system_prompt.py` | `SystemPrompt` aggregate, `SectionName` (StrEnum w/ `.order` + `.cache_group`), cache-placement policy | File I/O, repo-map building, AGENTS.md loading |
| `session.py` | JSONL append log, session resume, memory.md read/write | LLM calls, context window logic, tool execution |
| `loop.py` | `agent_loop()` async generator, all `AgentEvent` types, `AgentConfig` / `AgentResult`, cwd injection, retry policy, `Hooks` Protocol + `NoopHooks` | Session persistence, UI/ANSI, memory I/O |
| `prompts.py` | `build_chat_prompt()`, `build_autonomous_prompt()`, public composables (`tools_section`, `discover_skills`, `load_agents_doc`, `build_repo_map`, `environment_section`) | ANSI output, event handling, agent loop |
| `renderer.py` | ANSI helpers, `render_event()`, markdown printer, `print_footer()`, Rich console | Business logic, file I/O, agent-loop control |
| `runner.py` | `drive()` — drain `agent_loop` into an `AgentResult`, single seam used by both composition roots | Session persistence, KeyboardInterrupt handling |
| `chat.py` | Interactive REPL, paste handling, slash commands, `main` CLI entry | Anthropic wire format, tool implementations, direct `agent_loop` iteration (use `drive()`) |
| `autonomous.py` | `AutonomousFlow` state machine, git worktree lifecycle, `BashGuardHook`, delivery | Session JSONL persistence, REPL input, direct `agent_loop` iteration |

---

## Concept Index

Jump directly to the symbol — grep it in the file rather than scanning.

### Messages & token economics

| Concept | File → Symbol |
|---|---|
| all message types | `messages.py` → `UserMessage`, `AssistantMessage`, `ToolResultMessage`, `Message` union |
| content block types | `messages.py` → `TextContent`, `ThinkingContent`, `ToolCallContent`, `ImageContent`, `ContentBlock` union |
| token usage | `messages.py` → `TokenUsage`, `ZERO_USAGE` |
| tool plumbing | `messages.py` → `ToolResult`, `ToolDefinition` |
| system prompt section type | `messages.py` → `SystemPromptSection` |

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

### Tools

| Concept | File → Symbol |
|---|---|
| tool protocol | `tools.py` → `Tool` (Protocol) |
| tool registry | `tools.py` → `ToolRegistry`, `default_registry()` |
| path sandboxing | `tools.py` → `_sandbox()` |
| tool output cap (50 KB) | `tools.py` → `_MAX_OUTPUT`, `_cap()` |
| 6 tool implementations | `tools.py` → `BashTool` · `ReadTool` · `WriteTool` · `EditTool` · `GrepTool` · `FindTool` |

### Context window

| Concept | File → Symbol |
|---|---|
| context window aggregate | `context.py` → `ContextWindow` |
| context budget config | `context.py` → `ContextBudget`, `default_budget()` |
| context pressure tiers | `context.py` → `PressureTier`, `assess_pressure()` |
| pressure absolute thresholds | `context.py` → `ABSOLUTE_P4`, `ABSOLUTE_P3`, `ABSOLUTE_AGG` |
| P4 eviction (truncate old tool results) | `context.py` → `evict_p4()`, `_P4_MAX_BYTES`, `_P4_NOTICE` |
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
| session resume / deserialise | `session.py` → `resume_session()`, `_dict_to_msg()` |
| latest session for cwd | `session.py` → `latest_session_id()` |
| memory load (merged global+project) | `session.py` → `load_memory()`, `load_memory_deduped()` |
| memory update / dedup / cap | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS`, `_DEDUP_PREFIX` |

### Agent loop & hooks

| Concept | File → Symbol |
|---|---|
| agent loop entry point | `loop.py` → `agent_loop()` |
| agent convenience drain | `loop.py` → `run_agent()` |
| agent config factory | `loop.py` → `make_config()` |
| agent config / result | `loop.py` → `AgentConfig`, `AgentResult` |
| all agent event types | `loop.py` → `AgentEvent` union + ~14 frozen dataclasses above it |
| retry count / delay / jitter | `loop.py` → `_MAX_RETRIES`, `_BASE_DELAY`, `_MAX_DELAY`, `_retry_delay()` |
| tool result size cap (50 KB) | `loop.py` → `_MAX_TOOL_BYTES`, `_truncate_tool_result()` |
| cwd injection into tools | `loop.py` → `_CwdPatchedRegistry`, `_CwdBoundTool`, `make_config()` |
| hooks protocol | `loop.py` → `Hooks` (Protocol), `NoopHooks`, `HookDecision` |
| hook call sites | `loop.py` → `_hook_before_llm()`, `_hook_before_tool()`, `_hook_after_tool()` |

### Runner seam

| Concept | File → Symbol |
|---|---|
| drain agent_loop → AgentResult | `runner.py` → `drive()` |

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
| interactive REPL | `chat.py` → `run_chat()` |
| CLI entry point | `chat.py` → `main()` |
| paste collapse / expand | `chat.py` → `_make_paste_bindings()`, `_expand_pastes()` |
| single-prompt (non-interactive) | `chat.py` → `_run_single_prompt()` |
| end-of-session learning extraction | `chat.py` → `_extract_learnings()`, `_save_learnings()` |

### Autonomous

| Concept | File → Symbol |
|---|---|
| autonomous state machine | `autonomous.py` → `AutonomousFlow`, `FlowState` |
| destructive-bash guard hook | `autonomous.py` → `BashGuardHook` (subclass of `NoopHooks`) |
| gate checks (clean tree, not detached) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| git worktree create / cleanup | `autonomous.py` → `_create_worktree()`, `_cleanup_worktree()` |
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
| `messages.py` → `SystemPromptSection` | `system_prompt.py` (`SystemPrompt.build()` returns it) · `loop.py` (`AgentConfig` carries it) · `anthropic_provider.py` (consumes it in `stream()`) · `prompts.py` |
| `messages.py` → `TokenUsage` fields | `loop.py` (`AgentResult`) · `session.py` (`append_message`) · `renderer.py` (`print_footer`) · `chat.py` · `anthropic_provider.py` (`_extract_usage`) |
| `messages.py` → `ToolDefinition` | `tools.py` (`Tool.definition()`) · `loop.py` (tool_defs in `_stream_one_turn`) · `provider.py` (`LLMProvider.stream` signature) |
| `messages.py` → `ToolResult` | `tools.py` (every tool's `__call__` return) · `loop.py` (`_truncate_tool_result`) · `autonomous.py` (`BashGuardHook` synthesises one) |
| `models.py` → `Model` | `context.py` (`assess_pressure`, `default_budget`) · `loop.py` (`AgentConfig`) · `anthropic_provider.py` (`stream` signature) · `chat.py` · `autonomous.py` |
| `models.py` → `MODELS` | `chat.py` (/model slash command display) |
| `provider.py` → `LLMProvider` Protocol | `anthropic_provider.py` (must satisfy it) · `loop.py` (`_stream_one_turn` consumes it) · tests/`fake_provider.py` |
| `provider.py` → any `StreamEvent` type | `anthropic_provider.py` (yields them) · `loop.py` (`_stream_one_turn` switch) |
| `context.py` → `ContextWindow` interface | `chat.py` (`ctx.receive()`, `ctx.manage_pressure()`, `ctx.build_messages()`) · `autonomous.py` (not used directly today) |
| `context.py` → `PressureTier` / thresholds | — (internal to ContextWindow.manage_pressure) |
| `system_prompt.py` → `SystemPrompt` / `SectionName` | `prompts.py` (`build_chat_prompt`, `build_autonomous_prompt`) · `chat.py` (passes through) · `autonomous.py` (passes through) |
| `session.py` → JSONL entry format | `append_message()` + `_msg_to_dict()` (write side) · `resume_session()` + `_dict_to_msg()` (read side) — both sides must stay in sync · `tests/test_session_roundtrip.py` |
| `loop.py` → any `AgentEvent` type | `renderer.py` (`render_event` handles every branch) · `chat.py` · `autonomous.py` · `tests/fake_provider.py` (if it constructs them) |
| `loop.py` → `AgentConfig` fields | `make_config()` (same file) · `chat.py` (caller) · `autonomous.py` (caller) |
| `loop.py` → `AgentResult` fields | `chat.py` (result handling, session persistence) · `autonomous.py` (`_execute` returns it) · `runner.py` (transparent — just returns it) |
| `loop.py` → `Hooks` Protocol | `NoopHooks` (same file) · `BashGuardHook` (`autonomous.py`) · any new hook subclass · `tests/test_hooks.py` |
| `runner.py` → `drive()` signature | `chat.py` (two call sites: `run_chat`, `_run_single_prompt`) · `autonomous.py` (three call sites: `_plan`, `_execute`, `_verify_agent`) · `tests/test_runner.py` |
| `tools.py` → `Tool` protocol | All 6 tool classes in same file · `loop.py` (`_CwdBoundTool` proxy) |
| `tools.py` → `ToolRegistry` interface | `loop.py` (`_CwdPatchedRegistry`) · `chat.py` · `autonomous.py` · `prompts.py` (`tools_section`) |
| `prompts.py` → `build_chat_prompt` signature | `chat.py` (sole caller) |
| `prompts.py` → `build_autonomous_prompt` signature | `autonomous.py` (`_plan`, `_execute`, `_verify_agent`) |
| Add a new `SectionName` | `system_prompt.py` (enum variant + update `.order` + `.cache_group`) · `prompts.py` (`register()` call in the relevant builder) |
| Add a new tool | `tools.py` (`default_registry()`) · `prompts.py` (`tools_section` description) |
| Add a new provider adapter | New file `<vendor>_provider.py` satisfying `LLMProvider` · register/select in `chat.py` · `make_config(provider=…)` for tests |

---

## Common Extension Recipes

| Task | Steps |
|---|---|
| Add a new tool | Implement class in `tools.py` following `BashTool` pattern · add to `default_registry()` · add one-line description to `tools_section()` in `prompts.py` |
| Add a new model | Add entry to `MODELS` dict in `models.py` · update `DEFAULT_MODEL` if needed |
| Add a new system-prompt section | Add `SectionName` variant in `system_prompt.py` (set `.order` + `.cache_group` properties) · call `sp.register(SectionName.X, …)` in `prompts.py:build_chat_prompt()` and/or `build_autonomous_prompt()` |
| Add a new AgentEvent | Add frozen dataclass in `loop.py` · add to `AgentEvent` union · handle the new `isinstance` branch in `renderer.py:render_event()` |
| Change compaction logic | Implement a `CompactionPort` subclass · inject via `ContextWindow(compaction_port=…)` · `ContextWindow.manage_pressure()` will call it at P3/AGG |
| Add a Hook (e.g. policy / guard) | Subclass `NoopHooks` in any composition root · override `before_llm` / `before_tool_call` / `after_tool_call` · pass via `make_config(hooks=…)` |
| Add a new provider | Implement a class satisfying the `LLMProvider` Protocol in a sibling file · keep all SDK imports inside it · pass via `make_config(provider=…)` (no global state) |
| Inject a non-cached prompt section from a plugin | Call `sp.register_extra("name", lambda: text)` — appended after named sections, never cached |

---

## Python Conventions

- Python ≥ 3.12; use `X | Y` union syntax (not `Optional` / `Union`)
- `from __future__ import annotations` at the top of every file
- All public value objects: `@dataclass(frozen=True)`
- All tools: never raise — always return `ToolResult(is_error=True)` on failure
- Async all the way: tool execution, provider streaming, agent loop, REPL are all `async`
- Section headers in files: `# ── SectionName ──────────────────────────────────────────────`
- Module docstring in every file: purpose + dependency position (why it exists relative to its neighbours)

---

## Verification

Run these after every change.

```bash
# Install / reinstall the package in editable mode (uv)
uv pip install -e .

# Run the test suite (66 tests as of refactor/architecture-pass)
uv run pytest -q

# Check imports
python -c "import agent_forge; print('ok')"

# Smoke-test the CLI
agent-forge --help
```

---

## CLI Flags

```
agent-forge                         Interactive REPL
  --model <id>                      Model ID (default: claude-sonnet-4-6)
  --thinking off|adaptive|low|medium|high   Thinking budget (default: adaptive)
  --cwd <path>                      Working directory (default: $PWD)
  --continue                        Resume last session for this cwd
  --resume <id>                     Resume specific session (partial ID ok)
  --verbose                         Log context pressure tier, memory saves
  --prompt <text>                   Run single prompt non-interactively then exit

Slash commands (inside REPL):
  /quit  /exit  /q                  Exit and save learnings to memory
  /clear                            Clear conversation + context window
  /status                           Show session ID, token count, turn count
  /model                            Switch model interactively
```

Autonomous mode is invoked programmatically via `run_autonomous(AutonomousConfig(...))` — no CLI flag yet.

---

## Policies

| Policy | Location |
|---|---|
| Turn completeness: partial assistant messages never appended on error/abort | `loop.py` → `_stream_one_turn()` — only appends `assistant_msg` on `DoneEvent` |
| Abort completeness: remaining unexecuted tool calls get placeholder error results before `AbortedAgentEvent` | `loop.py` → tool execution loop, `tool_calls[i + 1:]` fill |
| Max-turns exits via `DoneAgentEvent(result.aborted=True)` — single exit path for callers | `loop.py` → end of `agent_loop()` while loop |
| Composition roots use `runner.drive()` to drain `agent_loop` — never iterate it directly | `chat.py` · `autonomous.py` (5 call sites total) |
| Tool result truncation at 50 KB (loop-time, before context append) | `loop.py` → `_truncate_tool_result()`, `_MAX_TOOL_BYTES` |
| Tool output cap at 50 KB (tool-time, before returning) | `tools.py` → `_cap()`, `_MAX_OUTPUT` |
| Path sandboxing (cwd enforcement, reject `../` escapes) | `tools.py` → `_sandbox()` |
| Retry: exponential backoff + jitter, max 3 attempts, max 30 s — owned by loop, NOT provider | `loop.py` → `_retry_delay()`, `_MAX_RETRIES`, `_MAX_DELAY` |
| Hooks default to `NoopHooks` — composition roots opt in by passing `make_config(hooks=…)` | `loop.py` → `AgentConfig.hooks` default · `autonomous.py` → `BashGuardHook` |
| Context pressure eviction (P4 inplace, P3/AGG compact) | `context.py` → `ContextWindow.manage_pressure()` · `assess_pressure()` · `evict_p4()` |
| ActionLog: evicted turns become one-liner summaries, never discarded | `context.py` → `ContextWindow.receive()` · `StratifiedWindowStrategy.summarise_turn()` |
| CompactionPort is optional — P3/AGG fall back to P4 if absent | `context.py` → `ContextWindow.manage_pressure()` |
| Cache placement: `cache_control=True` on last non-null section of each group | `system_prompt.py` → `SystemPrompt.build()` |
| Volatile sections (ENVIRONMENT, CUSTOM, plugin extras) never cached | `system_prompt.py` → `SectionName.is_volatile`, group 3 + `register_extra` |
| AGENTS.md → CLAUDE.md fallback, 32 KB cap, truncation notice | `prompts.py` → `load_agents_doc()` |
| Memory deduplication (60-char prefix match) | `session.py` → `load_memory_deduped()`, `_DEDUP_PREFIX` |
| Memory size cap (~2 K tokens) | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS` |
| Gate checks before worktree creation (clean tree, named branch) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| Worktree cleanup on success, failure, or crash | `autonomous.py` → `AutonomousFlow.run()` try/finally |
| Delivery only if all verify commands pass | `autonomous.py` → `FlowState` machine: VERIFYING before DELIVERING |
| Destructive Bash blocked in autonomous mode | `autonomous.py` → `BashGuardHook.before_tool_call()` |
| OAuth vs API key: different client, beta headers, system-as-user injection | `anthropic_provider.py` → `_is_oauth()` · `AnthropicProvider.stream()` · `_system_already_injected()` |

---

## Reference Documents

| Document | When to read it |
|---|---|
| `pyproject.toml` | Dependency versions, entry-point wiring, dev-tool config |
| `tests/fake_provider.py` | Reference implementation of `LLMProvider` for tests — copy this pattern when wiring a new provider |
| `tests/test_runner.py` | Canonical examples of how to drive `agent_loop` from a test |
| `tests/test_hooks.py` | Canonical examples of writing a custom `Hooks` subclass |
