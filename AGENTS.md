# Agent Instructions — agent_forge

A minimal Python coding agent: 7 flat modules, async-generator loop, interactive REPL (`agent-forge`) and autonomous git-isolated pipeline (`AutonomousFlow`).

---

## Module Dependency Order

Leaf → root. Lower modules must never import from higher ones.

```
provider.py          ← leaf: no internal imports
tools.py             ← provider
context.py           ← provider
session.py           ← provider
loop.py              ← provider · tools · context
prompts.py           ← context · session
renderer.py          ← loop · provider
chat.py              ← all modules (composition root — REPL)
autonomous.py        ← loop · context · prompts · tools · renderer (composition root — autonomous)
```

| Module | Owns | Must NOT contain |
|---|---|---|
| `provider.py` | Message types, Model catalog, TokenUsage, ToolDefinition, AnthropicProvider streaming | Any import from agent_forge |
| `tools.py` | Tool protocol, ToolRegistry, 6 built-in tools (Bash/Read/Write/Edit/Grep/Find) | LLM calls, session state, context logic |
| `context.py` | ContextWindow aggregate, SystemPrompt + SectionName, pressure tiers, P4 eviction, token estimation | File I/O, LLM calls, session persistence |
| `session.py` | JSONL append log, session resume, memory.md read/write | LLM calls, context window logic, tool execution |
| `loop.py` | `agent_loop()` async generator, all AgentEvent types, AgentConfig/AgentResult, cwd injection | Session persistence, UI/ANSI, memory I/O |
| `prompts.py` | SystemPrompt builder, AGENTS.md/repo-map loaders | ANSI output, event handling, agent loop |
| `renderer.py` | ANSI helpers, event renderer, markdown printer, footer | Business logic, file I/O |
| `chat.py` | Interactive REPL, paste handling, CLI entry point (`main`) | Anthropic wire format, tool implementations |
| `autonomous.py` | AutonomousFlow state machine, git worktree lifecycle, delivery | Session JSONL persistence, REPL input |

---

## Concept Index

Jump directly to the symbol — grep it in the file rather than scanning.

| Concept | File → Symbol |
|---|---|
| retry count / delay / jitter | `loop.py` → `_MAX_RETRIES`, `_BASE_DELAY`, `_MAX_DELAY`, `_retry_delay()` |
| tool result size cap (50 KB) | `loop.py` → `_MAX_TOOL_BYTES`, `_truncate_tool_result()` |
| cwd injection into tools | `loop.py` → `_CwdPatchedRegistry`, `_CwdBoundTool`, `make_config()` |
| all agent event types | `loop.py` → `AgentEvent` union + 12 frozen dataclasses above it |
| agent loop entry point | `loop.py` → `agent_loop()` |
| agent convenience drain | `loop.py` → `run_agent()` |
| agent config factory | `loop.py` → `make_config()` |
| all message types | `provider.py` → `UserMessage`, `AssistantMessage`, `ToolResultMessage` |
| content block types | `provider.py` → `TextContent`, `ThinkingContent`, `ToolCallContent` |
| model catalog / pricing | `provider.py` → `MODELS`, `DEFAULT_MODEL` |
| OAuth vs API key detection | `provider.py` → `_is_oauth()` |
| system prompt section type | `provider.py` → `SystemPromptSection` |
| Anthropic streaming adapter | `provider.py` → `AnthropicProvider.stream()`, `_do_stream()` |
| message → Anthropic API format | `provider.py` → `_to_api_messages()` |
| context pressure tiers | `context.py` → `PressureTier`, `assess_pressure()` |
| pressure absolute thresholds | `context.py` → `ABSOLUTE_P4`, `ABSOLUTE_P3`, `ABSOLUTE_AGG` |
| P4 eviction (truncate old tool results) | `context.py` → `evict_p4()`, `_P4_MAX_BYTES`, `_P4_NOTICE` |
| token estimation | `context.py` → `estimate_tokens()`, `estimate_tokens_list()` |
| context window aggregate | `context.py` → `ContextWindow` |
| context budget config | `context.py` → `ContextBudget`, `default_budget()` |
| action log eviction | `context.py` → `ContextWindow.receive()`, `ActionLogEntry`, `StratifiedWindowStrategy.summarise_turn()` |
| build LLM message array | `context.py` → `ContextWindow.build_messages()`, `StratifiedWindowStrategy.build()` |
| compaction port (DI boundary) | `context.py` → `CompactionPort`, `CompactionResult` |
| manage pressure (one-call facade) | `context.py` → `ContextWindow.manage_pressure()` |
| session resume from JSONL | `context.py` → `ContextWindow.init_from_existing()` |
| ordered system prompt sections | `context.py` → `SystemPrompt`, `SectionName` |
| cache group assignment | `context.py` → `SectionName.cache_group` property |
| cache placement (last per group) | `context.py` → `SystemPrompt.build()` |
| session JSONL write | `session.py` → `append_message()`, `append_metadata()`, `append_compaction()` |
| session directory path | `session.py` → `sessions_dir()` |
| session resume / deserialise | `session.py` → `resume_session()`, `_dict_to_msg()` |
| latest session for cwd | `session.py` → `latest_session_id()` |
| memory load (merged global+project) | `session.py` → `load_memory()`, `load_memory_deduped()` |
| memory update / dedup / cap | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS`, `_DEDUP_PREFIX` |
| tool protocol | `tools.py` → `Tool` (Protocol) |
| tool registry | `tools.py` → `ToolRegistry`, `default_registry()` |
| path sandboxing | `tools.py` → `_sandbox()` |
| tool output cap (50 KB) | `tools.py` → `_MAX_OUTPUT`, `_cap()` |
| 6 tool implementations | `tools.py` → `BashTool` · `ReadTool` · `WriteTool` · `EditTool` · `GrepTool` · `FindTool` |
| system prompt builder | `prompts.py` → `build_system_prompt()` |
| AGENTS.md / CLAUDE.md loader | `prompts.py` → `_load_agents_doc()` |
| repo map builder | `prompts.py` → `_build_repo_map()` |
| stable tools text (group 0 cache) | `prompts.py` → `_TOOLS_SECTION` |
| event renderer (ANSI) | `renderer.py` → `render_event()` |
| ANSI colour helpers | `renderer.py` → `dim()` · `bold()` · `green()` · `red()` · `yellow()` · `cyan()` |
| turn footer printer | `renderer.py` → `print_footer()` |
| Rich console singleton | `renderer.py` → `get_console()` |
| interactive REPL | `chat.py` → `run_chat()` |
| CLI entry point | `chat.py` → `main()` |
| paste collapse / expand | `chat.py` → `_make_paste_bindings()`, `_expand_pastes()` |
| single-prompt (non-interactive) | `chat.py` → `_run_single_prompt()` |
| end-of-session learning extraction | `chat.py` → `_extract_learnings()`, `_save_learnings()` |
| autonomous state machine | `autonomous.py` → `AutonomousFlow`, `FlowState` |
| gate checks (clean tree, not detached) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| git worktree create / cleanup | `autonomous.py` → `_create_worktree()`, `_cleanup_worktree()` |
| autonomous agent execution | `autonomous.py` → `AutonomousFlow._execute()` |
| verify commands runner | `autonomous.py` → `AutonomousFlow._verify()` |
| delivery (pr / merge / output / none) | `autonomous.py` → `AutonomousFlow._deliver()` |
| autonomous entry point | `autonomous.py` → `run_autonomous()` |

---

## Change Impact Map

When you change a type or interface, also update these downstream files.

| Changed | Also update |
|---|---|
| `provider.py` → any `Message` type | `context.py` · `session.py` · `loop.py` · `renderer.py` · `chat.py` · `autonomous.py` |
| `provider.py` → `SystemPromptSection` | `context.py` (`SystemPrompt.build()` returns it) · `loop.py` (`AgentConfig` carries it) · `prompts.py` |
| `provider.py` → `TokenUsage` fields | `loop.py` (`AgentResult`) · `session.py` (`append_message`) · `renderer.py` (`print_footer`) · `chat.py` |
| `provider.py` → `ToolDefinition` | `tools.py` (`Tool.definition()`) · `loop.py` (tool_defs in `_stream_one_turn`) |
| `provider.py` → `Model` | `context.py` (`assess_pressure`) · `loop.py` (`AgentConfig`) · `chat.py` · `autonomous.py` |
| `provider.py` → `MODELS` | `chat.py` (/model slash command display) |
| `context.py` → `ContextWindow` interface | `chat.py` (`ctx.receive()`, `ctx.manage_pressure()`, `ctx.build_messages()`) · `autonomous.py` (not used directly) |
| `context.py` → `SystemPrompt` / `SectionName` | `prompts.py` (`build_system_prompt`) · `autonomous.py` (inline sp) |
| `context.py` → `PressureTier` / thresholds | — |
| `session.py` → JSONL entry format | `append_message()` + `_msg_to_dict()` (write side) · `resume_session()` + `_dict_to_msg()` (read side) — both sides must stay in sync |
| `loop.py` → any `AgentEvent` type | `renderer.py` (`render_event` handles every branch) · `chat.py` · `autonomous.py` |
| `loop.py` → `AgentConfig` fields | `chat.py` (`make_config` call) · `autonomous.py` (`make_config` call) |
| `loop.py` → `AgentResult` fields | `chat.py` (result handling, session persistence) · `autonomous.py` (`_execute` returns it) |
| `tools.py` → `Tool` protocol | All 6 tool classes in same file · `loop.py` (`_CwdBoundTool`) |
| `tools.py` → `ToolRegistry` interface | `loop.py` (`_CwdPatchedRegistry`) · `chat.py` · `autonomous.py` · `prompts.py` |
| `prompts.py` → `build_system_prompt` signature | `chat.py` (sole caller for REPL) |
| Add a new `SectionName` | `context.py` (enum variant, `.order`, `.cache_group`) · `prompts.py` (`register()` call in `build_system_prompt`) |
| Add a new tool | `tools.py` (`default_registry()`) · `prompts.py` (`_TOOLS_SECTION` description) |

---

## Common Extension Recipes

| Task | Steps |
|---|---|
| Add a new tool | Implement class in `tools.py` following `BashTool` pattern · add to `default_registry()` · add one-line description to `_TOOLS_SECTION` in `prompts.py` |
| Add a new model | Add entry to `MODELS` dict in `provider.py` · update `DEFAULT_MODEL` if needed |
| Add a new system-prompt section | Add `SectionName` variant in `context.py` (set `order` + `cache_group` properties) · call `sp.register(SectionName.X, ...)` in `prompts.py:build_system_prompt()` |
| Add a new AgentEvent | Add frozen dataclass in `loop.py` · add to `AgentEvent` union · handle the new `isinstance` branch in `renderer.py:render_event()` |
| Change compaction logic | Implement a `CompactionPort` subclass · inject via `ContextWindow(compaction_port=...)` · `ContextWindow.manage_pressure()` will call it at P3/AGG |

---

## Python Conventions

- Python ≥ 3.12; use `X | Y` union syntax (not `Optional`/`Union`)
- `from __future__ import annotations` at the top of every file
- All public value objects: `@dataclass(frozen=True)`
- All tools: never raise — always return `ToolResult(is_error=True)` on failure
- Async all the way: tool execution, provider streaming, agent loop, REPL are all `async`
- Section headers in files: `# ── SectionName ──────────────────────────────────────────────`
- Module docstring in every file: purpose + dependency position (why it exists relative to its neighbors)

---

## Verification

Run these after every change.

```bash
# Install / reinstall the package in editable mode
uv pip install -e .

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
| Tool result truncation at 50 KB (loop-time, before context append) | `loop.py` → `_truncate_tool_result()`, `_MAX_TOOL_BYTES` |
| Tool output cap at 50 KB (tool-time, before returning) | `tools.py` → `_cap()`, `_MAX_OUTPUT` |
| Path sandboxing (cwd enforcement, reject `../` escapes) | `tools.py` → `_sandbox()` |
| Retry: exponential backoff + jitter, max 3 attempts, max 30 s | `loop.py` → `_retry_delay()`, `_MAX_RETRIES`, `_MAX_DELAY` |
| Context pressure eviction (P4 inplace, P3/AGG compact) | `context.py` → `ContextWindow.manage_pressure()` · `assess_pressure()` · `evict_p4()` |
| ActionLog: evicted turns become one-liner summaries, never discarded | `context.py` → `ContextWindow.receive()` · `StratifiedWindowStrategy.summarise_turn()` |
| CompactionPort is optional — P3/AGG fall back to P4 if absent | `context.py` → `ContextWindow.manage_pressure()` |
| Cache placement: `cache_control=True` on last non-null section of each group | `context.py` → `SystemPrompt.build()` |
| Volatile sections (ENVIRONMENT, CUSTOM) never cached | `context.py` → `SectionName.is_volatile`, `SectionName.cache_group == 3` |
| AGENTS.md → CLAUDE.md fallback, 32 KB cap, truncation notice | `prompts.py` → `_load_agents_doc()` |
| Memory deduplication (60-char prefix match) | `session.py` → `load_memory_deduped()`, `_DEDUP_PREFIX` |
| Memory size cap (~2 K tokens) | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS` |
| Gate checks before worktree creation (clean tree, named branch) | `autonomous.py` → `AutonomousFlow._gate_checks()` |
| Worktree cleanup on success, failure, or crash | `autonomous.py` → `AutonomousFlow.run()` try/finally |
| Delivery only if all verify commands pass | `autonomous.py` → `FlowState` machine: VERIFYING before DELIVERING |
| OAuth vs API key: different client, beta headers, system-as-user injection | `provider.py` → `_is_oauth()` · `AnthropicProvider.stream()` |

---

## Reference Documents

| Document | When to read it |
|---|---|
| `pyproject.toml` | Dependency versions, entry point wiring, dev tool config |
