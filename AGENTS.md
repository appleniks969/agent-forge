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
> | "How do I connect MCP servers?" | **[docs/user/mcp.md](docs/user/mcp.md)** |
> | "How do I use the wiki?" | **[README.md](README.md)** |
> | "If I change X, what else breaks?" | **AGENTS.md** (this file) |
> | "Where is symbol Y defined?" | **AGENTS.md** → Concept Index |
> | "What conventions does the code follow?" | **AGENTS.md** → Python Conventions |
> | "What invariants must I preserve?" | **AGENTS.md** → Policies |
>
> The user docs and AGENTS.md have ~no content overlap by design. CLI flags
> and slash commands live in `docs/user/` (single source of truth).

A minimal Python coding agent: 19 flat modules (incl. the optional
`mcp.py` integration), async-generator loop, interactive REPL
(`agent-forge`), and Model Context Protocol support. Reusable safety
hooks live in `guards.py`. Domain capabilities like the wiki ship as
separate Claude Code skills — see
[Wiki — extracted as a skill](#wiki--extracted-as-a-skill).

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
guards.py              ← messages · hooks  (BashGuardHook, PathGuardHook, MCPGuardHook, _CompositeHook — reusable safety hooks)
_subprocess.py         ← leaf: asyncio subprocess wrapper (signal/abort-aware)
tools.py               ← messages · _subprocess
mcp.py                 ← messages  (MCPSession Protocol, MCPClient, MCPManager, MCPTool — Tool-adapter; lazy-imports the optional `mcp` SDK only inside MCPClient.connect())
context.py             ← messages · models
system_prompt.py       ← messages
session.py             ← messages
loop.py                ← messages · models · provider · tools · events · hooks
prompts.py             ← messages · context · system_prompt · session · tools
runtime.py             ← messages · models · provider · context · system_prompt · tools · loop · (lazy) mcp
renderer.py            ← messages · loop
chat.py                ← all modules (composition root — REPL)
```

| Module | Owns | Must NOT contain |
|---|---|---|
| `messages.py` | `UserMessage` / `AssistantMessage` / `ToolResultMessage`, content blocks, `TokenUsage` / `ZERO_USAGE`, `ToolResult`, `ToolDefinition`, `SystemPromptSection` | Any internal import |
| `models.py` | `Model`, `ModelCost`, `MODELS` dict, `DEFAULT_MODEL` | Any internal import |
| `provider.py` | `LLMProvider` Protocol, 7 `StreamEvent` dataclasses (block-lifecycle) | The Anthropic SDK; concrete adapter logic; `AnthropicProvider` re-export |
| `anthropic_provider.py` | `AnthropicProvider`, OAuth/API-key dispatch, system-as-user injection, JSON repair, `_to_api_messages()` | Anything outside the Anthropic wire format |
| `events.py` | 16 `*AgentEvent` dataclasses + `AgentEvent` union + `ToolCallRecord` | Loop algorithm, hooks, provider plumbing |
| `hooks.py` | `Hooks` Protocol, `NoopHooks`, `HookDecision`, `AuditHook` (canonical observability example), `_hook_*` helpers | Loop algorithm, concrete *guard* hook subclasses (those live in `guards.py`) |
| `guards.py` | `BashGuardHook`, `PathGuardHook`, `MCPGuardHook`, `_CompositeHook` — reusable safety/policy hooks. All subclass `NoopHooks`; opt in via `AgentRuntime(hooks=…)` or `make_config(hooks=…)` | Composition-root specifics (worktrees, REPL, persistence); the loop algorithm; any provider/tool plumbing |
| `_subprocess.py` | `run()` async subprocess wrapper that races against an abort `asyncio.Event` and times out | Tool-specific logic; never raises for normal exits |
| `tools.py` | `Tool` Protocol, `ToolRegistry` (incl. `replace_mcp_tools()` + `mcp_names()` for MCP-source tagging), 6 built-in tools (Bash/Read/Write/Edit/Grep/Find), `_sandbox()`, `_cap()`, `sanitize_exception()` (LLM-safe error rendering) | LLM calls, session state, context logic, sync subprocess |
| `mcp.py` | `MCPSession` Protocol, `MCPClient` (one server lifecycle), `MCPManager` (many clients + namespacing), `MCPTool` adapter, `MCPServerConfig`, `MCPServerStatus`, `MCPToolDescriptor`, `namespaced_tool_name`/`unpack_namespaced_name`, TOML loader `load_mcp_configs`, CLI parser `parse_mcp_server_spec` | Top-level import of the `mcp` SDK (must be lazy inside `MCPClient.connect()`); UI/ANSI; agent-loop logic |
| `context.py` | `ContextWindow` aggregate, `PressureTier`, P4 eviction, token estimation, `CompactionPort`, `ContextBudget` (with `p4_max_bytes` / `tool_max_bytes`) | File I/O, LLM calls, session persistence, system-prompt assembly |
| `system_prompt.py` | `SystemPrompt` aggregate, `SectionName` (StrEnum w/ `.order` + `.cache_group`, incl. `MCP_TOOLS` cache-group-1), cache-placement policy, `invalidate_session()` covers MCP_TOOLS too | File I/O, repo-map building, AGENTS.md loading |
| `session.py` | JSONL append log, session resume, memory.md read/write, `index.json` cwd→session lookup | LLM calls, context window logic, tool execution |
| `loop.py` | `agent_loop()` async generator, `AgentConfig` / `AgentResult`, retry policy, tool-result truncation | Session persistence, UI/ANSI, memory I/O, `_CwdPatchedRegistry` (gone — `AgentConfig.cwd` is passed directly to `tool.execute`) |
| `prompts.py` | `build_chat_prompt()`, public composables (`tools_section`, `mcp_tools_section`, `discover_skills`, `load_agents_doc`, `build_repo_map`, `environment_section`) | ANSI output, event handling, agent loop |
| `runtime.py` | `AgentRuntime` — pairs a `ContextWindow` with `make_config()` and drains `agent_loop` inline. One `run_turn()` per user message, runs `manage_pressure()` automatically. Owns the optional `mcp_manager: MCPManager \| None`; `aclose()` chains its teardown. Also owns the **`build_runtime_with_mcp(...)`** factory | Session JSONL persistence, REPL input |
| `renderer.py` | ANSI helpers, `render_event()`, markdown printer, `print_footer()`, Rich console | Business logic, file I/O, agent-loop control |
| `chat.py` | Interactive REPL, paste handling, slash commands (`/quit /clear /status /model /remember /mcp`), CLI flags incl. `--mcp` / `--no-mcp` / `--mcp-server`, `main` CLI entry | Anthropic wire format, tool implementations, direct `agent_loop` iteration (use `AgentRuntime.run_turn()`) |

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

### Async subprocess utility

| Concept | File → Symbol |
|---|---|
| signal-aware subprocess runner | `_subprocess.py` → `run()` (returns `(returncode, stdout, stderr, aborted)`) |

### Tools

| Concept | File → Symbol |
|---|---|
| tool protocol | `tools.py` → `Tool` (Protocol) |
| tool registry | `tools.py` → `ToolRegistry`, `default_registry()` |
| MCP tool hot-swap | `tools.py` → `ToolRegistry.replace_mcp_tools()`, `ToolRegistry.mcp_names()` (tagged origin) |
| path sandboxing | `tools.py` → `_sandbox()` |
| tool output cap (50 KB) | `tools.py` → `_MAX_OUTPUT`, `_cap()` |
| 6 tool implementations | `tools.py` → `BashTool` · `ReadTool` · `WriteTool` · `EditTool` · `GrepTool` · `FindTool` |
| edit overlap detection | `tools.py` → `EditTool.execute()` (Phase 1.5 in the two-phase commit) |

### MCP (Model Context Protocol)

| Concept | File → Symbol |
|---|---|
| MCP session protocol (test seam) | `mcp.py` → `MCPSession` (Protocol with `initialize` / `list_tools` / `call_tool`) |
| MCP server lifecycle (one server) | `mcp.py` → `MCPClient` (connect / reconnect / aclose / status) |
| MCP server status enum | `mcp.py` → `MCPServerStatus` (`disconnected` · `connecting` · `connected` · `failed` · `closed`) |
| MCP multi-server manager | `mcp.py` → `MCPManager` (owns clients + namespacing) |
| MCP tool adapter (satisfies `Tool` Protocol) | `mcp.py` → `MCPTool` |
| MCP tool descriptor (server-reported) | `mcp.py` → `MCPToolDescriptor` |
| MCP server config | `mcp.py` → `MCPServerConfig` |
| MCP name namespacing | `mcp.py` → `namespaced_tool_name()`, `unpack_namespaced_name()` (`{server}__{tool}`) |
| MCP TOML config loader | `mcp.py` → `load_mcp_configs(cwd)` (`~/.agent-forge/mcp.toml` + `<cwd>/.agent-forge/mcp.toml`) |
| MCP CLI spec parser (`--mcp-server`) | `mcp.py` → `parse_mcp_server_spec()` |
| Lazy SDK import (the optional `mcp` SDK) | `mcp.py` → inside `MCPClient.connect()` only |
| Composition factory (MCP-aware runtime) | `runtime.py` → `build_runtime_with_mcp()` |
| MCP tools prompt section | `prompts.py` → `mcp_tools_section()` (grouped by server) |
| MCP tools section enum | `system_prompt.py` → `SectionName.MCP_TOOLS` (order 3, cache_group 1, non-volatile) |
| Safety guard for destructive MCP calls | `guards.py` → `MCPGuardHook` (token-based destructive-verb veto) |
| `/mcp` slash commands | `chat.py` → `_handle_mcp_command()`, `_format_mcp_status()` |
| MCP server status callback | `runtime.py` → `build_runtime_with_mcp(on_status=...)` |
| MCP config resolution (file + CLI merge) | `chat.py` → `_resolve_mcp_configs()` |

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
| audit hook (observability) | `hooks.py` → `AuditHook` (subclass of `NoopHooks`; emits before/after via `logging.getLogger("agent_forge.audit")`; `redact_args`, `include_result_preview`, custom `logger` / `level`) |
| LLM-safe error rendering | `tools.py` → `sanitize_exception()` (class-name prefix + `$HOME` → `~`, never includes a traceback) |

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

### Runtime seam

| Concept | File → Symbol |
|---|---|
| per-session glue (ctx + cfg factory + drive) | `runtime.py` → `AgentRuntime` (`run_turn()`, `clear()`, `init_messages()`) |

### Prompts (composition)

| Concept | File → Symbol |
|---|---|
| REPL system-prompt builder | `prompts.py` → `build_chat_prompt()` |
| stable tools text section | `prompts.py` → `tools_section()` |
| skills discovery | `prompts.py` → `discover_skills()` |
| AGENTS.md / CLAUDE.md loader | `prompts.py` → `load_agents_doc()` |
| repo map builder | `prompts.py` → `build_repo_map()` |
| environment section (cwd/branch/date) | `prompts.py` → `environment_section()` |
| identity / guidelines text constants | `prompts.py` → `CHAT_IDENTITY`, `CHAT_GUIDELINES` |

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

### Guards (reusable safety hooks)

| Concept | File → Symbol |
|---|---|
| destructive-bash guard hook | `guards.py` → `BashGuardHook` (subclass of `NoopHooks`) |
| sensitive-path guard hook | `guards.py` → `PathGuardHook` (subclass of `NoopHooks`) |
| destructive-MCP guard hook | `guards.py` → `MCPGuardHook` (token-based veto; `extra_verbs` / `extra_prefixes` / `allow_servers`) |
| compose multiple hooks | `guards.py` → `_CompositeHook` |

---

## Change Impact Map

When you change a type or interface, also update these downstream files.

| Changed | Also update |
|---|---|
| `messages.py` → any `Message` type | `context.py` · `session.py` · `loop.py` · `anthropic_provider.py` · `renderer.py` · `chat.py` |
| `messages.py` → `UserMessage.content` (vision) | `session.py` (`_msg_to_dict` / `_dict_to_msg`) · `anthropic_provider.py` (`_to_api_messages`) · `context.py` (`estimate_tokens`) · `tests/test_session_roundtrip.py` |
| `messages.py` → `SystemPromptSection` | `system_prompt.py` (`SystemPrompt.build()` returns it) · `loop.py` (`AgentConfig` carries it) · `anthropic_provider.py` (consumes it in `stream()`) · `prompts.py` |
| `messages.py` → `TokenUsage` fields | `loop.py` (`AgentResult`) · `session.py` (`append_message` + `resume_session` re-stitch) · `renderer.py` (`print_footer`) · `chat.py` · `anthropic_provider.py` (`_extract_usage`) |
| `messages.py` → `ToolDefinition` | `tools.py` (`Tool.definition()`) · `loop.py` (tool_defs in `_stream_one_turn`) · `provider.py` (`LLMProvider.stream` signature) |
| `messages.py` → `ToolResult` | `tools.py` (every tool's `__call__` return) · `loop.py` (`_truncate_tool_result`) · `guards.py` (hooks may synthesise one via `HookDecision`) |
| `models.py` → `Model` | `context.py` (`assess_pressure`, `default_budget`) · `loop.py` (`AgentConfig`) · `anthropic_provider.py` (`stream` signature) · `chat.py` · `runtime.py` |
| `models.py` → `MODELS` | `chat.py` (/model slash command display) |
| `provider.py` → `LLMProvider` Protocol | `anthropic_provider.py` (must satisfy it) · `loop.py` (`_stream_one_turn` consumes it) · `runtime.py` (carries it) · `tests/fake_provider.py` |
| `provider.py` → any `StreamEvent` type | `anthropic_provider.py` (yields them) · `loop.py` (`_stream_one_turn` switch) |
| `events.py` → any `*AgentEvent` | `renderer.py` (`render_event` handles every branch) · `chat.py` · `tests/fake_provider.py` |
| `hooks.py` → `Hooks` Protocol | `NoopHooks` (same file) · `BashGuardHook` / `PathGuardHook` / `MCPGuardHook` / `_CompositeHook` (`guards.py`) · any new hook subclass · `tests/test_hooks.py` |
| `_subprocess.py` → `run()` signature | `tools.py` (`BashTool`, `GrepTool` rg fallback) |
| `context.py` → `ContextWindow` interface | `runtime.py` (sole user inside the package) · `chat.py` only via `runtime.context.*` getters |
| `context.py` → `ContextBudget` fields | `default_budget()` (same file) · `evict_p4()` consumers if a new threshold is added |
| `system_prompt.py` → `SystemPrompt` / `SectionName` | `prompts.py` (`build_chat_prompt`) · `runtime.py` (carries it) · `chat.py` (passes through to runtime) |
| `session.py` → JSONL entry format | `append_message()` + `_msg_to_dict()` (write side) · `resume_session()` + `_dict_to_msg()` (read side) — both sides must stay in sync · `tests/test_session_roundtrip.py` |
| `session.py` → `index.json` schema | `_read_index()` / `_write_index()` / `latest_session_id()` (rebuild path) · `tests/test_phase6.py` |
| `loop.py` → `AgentConfig` fields | `make_config()` (same file) · `runtime.py` (`AgentRuntime.make_cfg()`) · tests using `make_config(...)` directly |
| `loop.py` → `AgentResult` fields | `chat.py` (result handling, session persistence) · `runtime.py` (transparent) |
| `runtime.py` → `AgentRuntime` API | `chat.py` (REPL: `run_turn`, `clear`, `init_messages`, `context`) |
| `tools.py` → `Tool` protocol | All 6 tool classes in same file · `loop.py` calls `tool.execute(args, cwd=…, signal=…)` directly |
| `tools.py` → `ToolRegistry` interface | `loop.py` (`config.tool_registry.get / definitions`) · `runtime.py` · `chat.py` · `prompts.py` (`tools_section` + `mcp_tools_section`) · `mcp.py` (`MCPManager` calls `replace_mcp_tools`) |
| `prompts.py` → `build_chat_prompt` signature | `chat.py` (sole caller) |
| Add a new `SectionName` | `system_prompt.py` (enum variant + update `.order` + `.cache_group` + decide `is_volatile`) · `prompts.py` (`register()` call in the relevant builder) · update `invalidate_session()` if the section is session-stable (cache group 1+2) |
| Add a new tool | `tools.py` (`default_registry()`) · `prompts.py` (`tools_section` description) |
| Add a new provider adapter | New file `<vendor>_provider.py` satisfying `LLMProvider` · register/select in `chat.py` · `make_config(provider=…)` for tests |
| `mcp.py` → `MCPSession` Protocol | `MCPClient.connect()` (provides the SDK session) · `tests/fake_mcp_server.py` (test double) — both must satisfy the same three methods |
| `mcp.py` → `MCPServerConfig` fields | `load_mcp_configs()` TOML parsing · `parse_mcp_server_spec()` CLI parsing · `MCPClient.__init__` consumption · `tests/test_phase_g.py` / `tests/test_phase_h.py` |
| `mcp.py` → namespacing convention (`{server}__{tool}`) | `MCPTool.name` · `mcp_tools_section()` (group-by-server) · `guards.py:MCPGuardHook` (split on `__`) · `ToolRegistry.mcp_names()` (tag) — all four sites assume the same convention |
| `mcp.py` → `MCPManager.tools()` return | `build_runtime_with_mcp()` (hot-loads into registry) · `/mcp tools` slash command · prompt's MCP_TOOLS section |
| `runtime.py` → `build_runtime_with_mcp()` signature | `chat.py` (`run_chat` + `_run_single_prompt`) — both composition roots call it |
| `runtime.py` → `AgentRuntime.mcp_manager` attribute | `chat.py` (`_handle_mcp_command`, `_format_mcp_status`) · runtime's own `aclose()` chain · any future programmatic caller |
| Add a new MCP guard policy | Subclass `NoopHooks` (or extend `MCPGuardHook`) · install via `_CompositeHook(BashGuardHook(), PathGuardHook(), MCPGuardHook(), YourHook())` and pass through `AgentRuntime(hooks=…)` |

---

## Common Extension Recipes

| Task | Steps |
|---|---|
| Add a new tool | Implement class in `tools.py` following `BashTool` pattern (use `_subprocess.run` for shell-out; never `subprocess.run`) · add to `default_registry()` · add one-line description to `tools_section()` in `prompts.py` |
| Add a new model | Add entry to `MODELS` dict in `models.py` · update `DEFAULT_MODEL` if needed |
| Add a new system-prompt section | Add `SectionName` variant in `system_prompt.py` (set `.order` + `.cache_group` properties) · call `sp.register(SectionName.X, …)` in `prompts.py:build_chat_prompt()` |
| Add a new AgentEvent | Add frozen dataclass in `events.py` · add to `AgentEvent` union · handle the new `isinstance` branch in `renderer.py:render_event()` |
| Change compaction logic | Implement a `CompactionPort` subclass · inject via `ContextWindow(compaction_port=…)` · `ContextWindow.manage_pressure()` will call it at P3/AGG |
| Add a Hook (e.g. policy / guard) | Subclass `NoopHooks` in any composition root · override `before_llm` / `before_tool_call` / `after_tool_call` · pass via `make_config(hooks=…)` (or `_CompositeHook(...)` to chain). For observability, `AuditHook` is the ready-made canonical example — pass it directly or chain inside `_CompositeHook(AuditHook(), BashGuardHook(), …)` |
| Add a new provider | Implement a class satisfying the `LLMProvider` Protocol in a sibling file · keep all SDK imports inside it · pass via `make_config(provider=…)` (no global state) · or pass via `AgentRuntime(provider=…)` |
| Inject a non-cached prompt section from a plugin | Call `sp.register_extra("name", lambda: text)` — appended after named sections, never cached |
| Tighten the tool-result truncation cap | Pass `tool_max_bytes` via `make_config(...)` or set `AgentConfig.tool_max_bytes` directly (default 50 KB) |
| Tighten the P4 eviction threshold | Construct a custom `ContextBudget(p4_max_bytes=…)` and pass to `ContextWindow(budget=…)` |
| Add an MCP server (user, not code) | Drop a TOML stanza into `~/.agent-forge/mcp.toml` or `<cwd>/.agent-forge/mcp.toml`. Schema and live-reload in **[docs/user/mcp.md](docs/user/mcp.md)**. No code changes. |
| Add an MCP server (programmatic) | Construct an `MCPServerConfig(name=, command=, args=, env=)` · pass via `mcp_configs=[…]` to `build_runtime_with_mcp()` · the manager registers tools on connect, the runtime closes them on `aclose()` |
| Add a custom MCP-aware guard policy | Subclass `NoopHooks` and inspect `call.name` for the `"__"` separator (or call `unpack_namespaced_name()` from `mcp.py`) · install via `_CompositeHook(..., YourHook())` and pass through `AgentRuntime(hooks=…)` |
| Stub MCP in tests | Use `tests/fake_mcp_server.py:FakeMCPSession` + `fake_session_factory({"name": session})` · inject via `MCPManager(configs, session_factory=…)` — production code path never imports the real SDK |

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

# Run the test suite (~529 tests: 319 agent-forge core + 210 wiki skill)
uv run pytest -q

# Check imports
python -c "import agent_forge; print('ok')"

# Smoke-test the CLI
agent-forge --help
```

---

## CLI Flags & Slash Commands

User-facing reference lives in **[docs/user/configuration.md](docs/user/configuration.md)** (CLI flags), **[docs/user/faq.md](docs/user/faq.md)** (slash commands), and **[docs/user/mcp.md](docs/user/mcp.md)** (MCP servers, TOML schema, `/mcp` subcommands). This file does not duplicate them — see the user docs to keep one source of truth.

Internal contract for contributors:

- `argparse` choices for `--thinking` are defined in `chat.py:_parse_args()`.
  When you add a new level, also update `docs/user/configuration.md` (CLI
  flags reference + Thinking modes table).
- Slash commands are dispatched in `chat.py:run_chat()`. New slash commands
  must (a) appear in the slash-command table in `docs/user/faq.md` and
  (b) update `_status_text()` if they affect session state.
- MCP CLI flags (`--mcp`, `--no-mcp`, `--mcp-server`) live in
  `chat.py:_parse_args()`. The `--mcp-server SPEC` format is parsed by
  `mcp.py:parse_mcp_server_spec()`. When changing either, update
  `docs/user/mcp.md` *and* `docs/user/configuration.md` (CLI flags table).
- MCP slash commands (`/mcp`, `/mcp tools`, `/mcp reconnect [name]`) are
  dispatched by `chat.py:_handle_mcp_command()`. New subcommands must
  appear in `docs/user/mcp.md` *and* `docs/user/faq.md`.

---

## Policies

| Policy | Location |
|---|---|
| Turn completeness: partial assistant messages never appended on error/abort | `loop.py` → `_stream_one_turn()` — only appends `assistant_msg` on `DoneEvent` |
| Abort completeness: remaining unexecuted tool calls get placeholder error results before `AbortedAgentEvent` | `loop.py` → tool execution loop, `tool_calls[i + 1:]` fill |
| Max-turns exits via `DoneAgentEvent(result.aborted=True)` — single exit path for callers | `loop.py` → end of `agent_loop()` while loop |
| Composition root uses `AgentRuntime.run_turn()` to drive a turn — never iterates `agent_loop` directly (only the internal drain inside `runtime.run_turn()` does) | `chat.py` (REPL + `_run_single_prompt`) |
| Tool result truncation default 50 KB (loop-time, before context append; configurable per `AgentConfig.tool_max_bytes`) | `loop.py` → `_truncate_tool_result()`, `_MAX_TOOL_BYTES` |
| Tool exception sanitization: every last-resort `except Exception` in `tools.py` + `loop.py` funnels through `sanitize_exception()` — prefixes the exception class name and redacts `$HOME`. Never includes a traceback (would leak site-packages paths). Boundary is "data leaving toward the LLM"; user-facing renderer errors stay verbatim. | `tools.py` → `sanitize_exception()`; call sites: 6× `tools.py` + 1× `loop.py:_stream_one_turn` |
| Tool output cap at 50 KB (tool-time, before returning) | `tools.py` → `_cap()`, `_MAX_OUTPUT` |
| Path sandboxing (cwd enforcement, reject `../` escapes) | `tools.py` → `_sandbox()` |
| Edit overlap detection (identical or nested old_strings rejected) | `tools.py` → `EditTool.execute()` (Phase 1.5 of two-phase commit) |
| All shell-outs are async + signal-aware (kill on abort, kill on timeout) | `_subprocess.py` → `run()` |
| Retry: exponential backoff + jitter, max 3 attempts, max 30 s — owned by loop, NOT provider | `loop.py` → `_retry_delay()`, `_MAX_RETRIES`, `_MAX_DELAY` |
| Hooks default to `NoopHooks` — composition roots opt in by passing `make_config(hooks=…)` or `AgentRuntime(hooks=…)` | `loop.py` → `AgentConfig.hooks` default · `guards.py` → `_CompositeHook(BashGuardHook(), PathGuardHook(), MCPGuardHook())` as the canonical composition |
| Context pressure eviction (P4 inplace, P3/AGG compact) — runs after every turn via `AgentRuntime.run_turn()` | `runtime.py` → `run_turn()` calls `ctx.manage_pressure()` · `context.py` → `ContextWindow.manage_pressure()` |
| ActionLog: evicted turns become one-liner summaries, never discarded | `context.py` → `ContextWindow.receive()` · `StratifiedWindowStrategy.summarise_turn()` |
| CompactionPort is optional — P3/AGG fall back to P4 if absent | `context.py` → `ContextWindow.manage_pressure()` |
| Cache placement: `cache_control=True` on last non-null section of each group (advisory hint; providers may ignore) | `system_prompt.py` → `SystemPrompt.build()` · `messages.py` → `SystemPromptSection.cache_control` |
| Volatile sections (ENVIRONMENT, CUSTOM, plugin extras) never cached | `system_prompt.py` → `SectionName.is_volatile`, group 3 + `register_extra` |
| AGENTS.md → CLAUDE.md fallback, 32 KB cap, truncation notice | `prompts.py` → `load_agents_doc()` |
| Memory deduplication (60-char prefix match) | `session.py` → `load_memory_deduped()`, `_DEDUP_PREFIX` |
| Memory size cap (~2 K tokens) | `session.py` → `update_memory()`, `_MEMORY_CAP_TOKENS` |
| Session resume re-stitches outer-entry usage onto `AssistantMessage.usage` | `session.py` → `resume_session()` |
| `latest_session_id()` is O(1) via `~/.agent-forge/sessions/index.json`, with O(n) scan fallback | `session.py` → `_read_index()`, `_write_index()`, `latest_session_id()` |
| Destructive Bash blocked (when guard opted in) | `guards.py` → `BashGuardHook.before_tool_call()` |
| Sensitive-path writes blocked (when guard opted in) | `guards.py` → `PathGuardHook.before_tool_call()` |
| Destructive MCP tool calls blocked (when guard opted in) | `guards.py` → `MCPGuardHook.before_tool_call()` (token-based on `{server}__{tool}`) |
| OAuth vs API key: different client, beta headers, system-as-user injection | `anthropic_provider.py` → `_is_oauth()` · `AnthropicProvider.stream()` · `_system_already_injected()` |
| `import agent_forge` works without the Anthropic SDK installed (best-effort import) | `__init__.py` → `try: from .anthropic_provider import AnthropicProvider` |
| `import agent_forge.mcp` works without the optional `mcp` SDK installed (lazy import inside `MCPClient.connect()`) | `mcp.py` → SDK imports gated inside the default `_default_session_factory` |
| MCP server failures are reported via `MCPServerStatus`, never raised — a broken server doesn't stop startup | `mcp.py` → `MCPClient.connect()` catches; `MCPManager.connect_all()` continues |
| MCP tool names are namespaced `{server}__{tool}` everywhere they appear (registry, prompt, guard, slash commands) | `mcp.py` → `namespaced_tool_name()` is the single source of truth |
| MCP_TOOLS sits in cache group 1 (session-stable), invalidated on `/clear` and `/mcp reconnect`, never on every turn | `system_prompt.py` → `SectionName.MCP_TOOLS`, `SystemPrompt.invalidate_session()` · `chat.py` → `_handle_mcp_command()` |
| The user docs (`docs/user/mcp.md`) are the single source of truth for the TOML schema + CLI flags + slash commands | Update those files when changing MCP user-visible surface; this file just points there |

---

## Wiki — extracted as a skill

**The wiki is no longer part of agent-forge core.** It was extracted in
ADR-005 to a self-contained Claude Code skill at
`.claude/skills/agent-forge-wiki/`.

The agent-forge package has no imports from the wiki; the wiki has no
required imports from agent-forge (it soft-deps on `agent_forge.{messages,
models, provider}` for OAuth dispatch + retry, until that's replaced with
a direct anthropic SDK call in a follow-up).

For the wiki's internal architecture (six stages, schema'd bundles, change
impact, dependency rules), see:

- `.claude/skills/agent-forge-wiki/SKILL.md` — entry point, when to invoke
- `.claude/skills/agent-forge-wiki/scripts/wiki/` — the implementation
- `.claude/skills/agent-forge-wiki/tests/` — wiki tests (run with pytest)
- `docs/adr/ADR-005-wiki-extracted-as-skill.md` — the decision

For end-user docs (how to gather, compile, contexts.yaml), see the
**"Wiki skill"** section in [README.md](README.md).

---

## Reference Documents

| Document | When to read it |
|---|---|
| `pyproject.toml` | Dependency versions, entry-point wiring, dev-tool config |
| `tests/fake_provider.py` | Reference implementation of `LLMProvider` for tests — copy this pattern when wiring a new provider |
| `tests/fake_mcp_server.py` | Reference `MCPSession` test double — copy when adding an MCP-touching test |
| `tests/test_hooks.py` | Canonical examples of writing a custom `Hooks` subclass |
| `tests/test_phase6.py` | Examples of `PathGuardHook`, `EditTool` overlap detection, session index tests |
| `tests/test_phase_g.py` | Canonical `MCPClient` / `MCPManager` test patterns (connect, reconnect, namespacing, aclose) |
| `tests/test_phase_h.py` | TOML loader, `--mcp-server` parser, `build_runtime_with_mcp` factory, `/mcp` slash commands |
| `tests/test_phase_i.py` | `MCPGuardHook` policy, `SectionName.MCP_TOOLS` caching, `mcp_tools_section()` grouping |
| `tests/test_phase_k.py` | `sanitize_exception()` (home-dir redaction, class-name prefix), `AuditHook` (logging shape, redact-args, composition with guards), ADR-006 v1-scope guard |
| `docs/user/mcp.md` | End-user MCP how-to: TOML schema, CLI flags, slash commands, troubleshooting |

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

### Connect an MCP server (user-facing TOML)

End-users: this is a TOML edit, not a code change. Drop into
`~/.agent-forge/mcp.toml` (global) or `<cwd>/.agent-forge/mcp.toml`
(project) — see **[docs/user/mcp.md](docs/user/mcp.md)** for the full
schema. The REPL loads them automatically; `/mcp` shows status.

```toml
[servers.fs]
command = "mcp-server-filesystem"
args    = ["/home/me/projects"]

[servers.gh]
command = "mcp-server-github"
env     = { GITHUB_TOKEN = "ghp_xxx" }
```

### Connect an MCP server programmatically

When using agent-forge as a library, drive the factory directly. Note
the `async with` — it closes every MCP child process on exit.

```python
import asyncio
from agent_forge import (
    build_runtime_with_mcp, ChatConfig, MCPServerConfig,
    UserMessage, default_registry,
)
from agent_forge.prompts import build_chat_prompt_async


async def main() -> None:
    cfg = ChatConfig(api_key="...", cwd=".")
    tool_registry = default_registry()
    system_prompt = await build_chat_prompt_async(cfg, tool_registry)

    mcp_servers = [
        MCPServerConfig(name="fs", command="mcp-server-filesystem", args=("/tmp",)),
    ]

    async with await build_runtime_with_mcp(
        model=cfg.model, system_prompt=system_prompt,
        tool_registry=tool_registry, cwd=cfg.cwd,
        mcp_configs=mcp_servers, api_key=cfg.api_key,
    ) as runtime:
        result = await runtime.run_turn(UserMessage(content="list /tmp"))
        print(result.text)


asyncio.run(main())
```

### Write a custom MCP guard policy

Subclass `NoopHooks` (or extend `MCPGuardHook`) and install via
`_CompositeHook`. Example: block any MCP write to a production database
even though the verb wouldn't trip the default heuristic.

```python
from agent_forge import NoopHooks, HookDecision
from agent_forge.messages import ToolCallContent
from agent_forge.mcp import unpack_namespaced_name


class ProdDBGuard(NoopHooks):
    """Refuse mutating calls on the prod_db MCP server."""

    _MUTATING = frozenset({"insert", "update", "upsert", "exec", "execute"})

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        parsed = unpack_namespaced_name(call.name)
        if parsed is None:
            return None     # not an MCP call
        server, tool = parsed
        if server != "prod_db":
            return None
        if any(verb in tool.lower() for verb in self._MUTATING):
            return HookDecision(
                block=True,
                reason=f"mutating prod_db calls are blocked: {call.name!r}",
            )
        return None
```

Install it by passing through `AgentRuntime`:

```python
runtime = AgentRuntime(..., hooks=_CompositeHook(
    BashGuardHook(), PathGuardHook(), MCPGuardHook(), ProdDBGuard(),
))
```

For a single guard, you can skip the composite:

```python
runtime = AgentRuntime(..., hooks=ProdDBGuard())
```

### Enable tool-call audit logging

`AuditHook` is the ready-made observability example — it emits one log
record before each tool call and one after, at `INFO` on the
`agent_forge.audit` logger.

```python
import logging
from agent_forge import AuditHook, AgentRuntime

logging.basicConfig(level=logging.INFO, format="%(message)s")

runtime = AgentRuntime(..., hooks=AuditHook())
# Default: args are redacted to {key-set-only}, results not logged.
# For richer observability:
runtime = AgentRuntime(..., hooks=AuditHook(
    redact_args=False,             # log full args (trusted envs only)
    include_result_preview=True,   # log first 200 chars of every result
    level=logging.DEBUG,
    logger=logging.getLogger("myapp.tools"),
))
```

To compose with a guard:

```python
from agent_forge.guards import _CompositeHook, BashGuardHook, PathGuardHook
hooks = _CompositeHook(AuditHook(), BashGuardHook(), PathGuardHook())
```

Log line shape:

```
[audit] turn=1 tool=Bash args={command} id=toolu_01ABC
[audit] turn=1 tool=Bash ok duration_ms=42 id=toolu_01ABC
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
