# agent-forge v2 — Final Architecture
**A sans-IO kernel over one event log** (forge/k chassis, panel grafts applied)

---

## 1. Organizing principle

The agent is a pure transition function — `step(state, input, policy) -> (state, effects, events)` — executed by a deliberately dumb async driver, and **the append-only event log is the only source of truth**. Every fact that changes a session (user input, completed model output, tool result, permission decision, compaction summary) is an event emitted by exactly one producer (the kernel), fsync'd before anything else sees it; transcript, context window, renderer, resume, telemetry, eval, and the wiki are all folds or subscribers over that log. Streaming deltas are the one deliberate exception: they go straight to a render callback and never enter kernel state, so the provider seam shrinks to "give me one completed output," the Anthropic block-lifecycle dialect dies, and retry-after-partial-stream cannot half-mutate anything. The scoping razor for the whole system: **if a feature can be a separate program reading the log, it is not in the core.**

### Calls made on contested points

The panel split 2–1 (pragmatism + conceptual integrity for forge/k, evolution for Hexforge); merged scores also favor forge/k. I take the forge/k chassis and resolve Judge 2's four evolution objections by graft rather than by switching chassis:

| Contested point | Call | Why |
|---|---|---|
| Sync bus vs async delivery | **Ship the bounded-queue `AsyncIterator` now** (Hexforge), not "a bridge we deliberately don't ship" | The sync-callback-blocks-the-loop defect is a *named* pain point of the current system; refusing the 30-line fix re-creates it |
| Deltas in kernel state vs render-only | **Render-only** (forge/k) | Judges 1+3 are right that this is the deepest cut: it deletes the dialect *and* the retry-dedup hole. The cost — abort discards partial text — is acceptable for a coding agent and stated as a property, not a bug |
| Redaction location | **Rewrite-before-append** (Thin-Waist, per Judge 3), not persister-side | Disk, wire, and model always agree; the transient-vs-persisted hook split that today survives only by docstring disappears. We accept that the model also sees redacted results — for secrets that's a feature |
| ro/rw vs Effects flags | **Effects flag enum** (Hexforge) — all three judges converged | One declaration drives scheduling and guarding; `EXTERNAL ⇒ Ask` blunts the lying-MCP-server hole |
| Sub-agents | **Provision, don't build**: `sid`/`parent` on every envelope + a `ChildSpawned` event (Ledger, per Judge 2) | Costs two fields now; buys the child-stream model later without a renovation |
| MCP-for-builtins | **Rejected** (Judges 1+3) | JSON framing on every Read is a hot-path tax for zero current plugin authors, and it surrenders per-call `cwd`/cancel injection — the old design's cleanest mechanism |
| Prompt memoization key | **Content hash**, not mtime (Judge 3 over forge/k's own text) | mtime granularity and non-file state are exactly the cache-key bugs forge/k self-declared |
| Clock port | **Dropped** (Hexforge ceremony) | The retry decorator takes an injectable `sleep`; that's the whole testability requirement |

---

## 2. Package layout

```
forge/
  kernel/            # pure, synchronous, stdlib-only — zero internal deps
    types.py         #   Message, Block, ToolCall, ToolResult, Usage, Effort — all frozen
    events.py        #   THE Event union + Envelope(seq, sid, parent, v, durable) + serde
    state.py         #   SessionState + fold(envelopes) -> SessionState; resume == live
    step.py          #   step(state, input, policy) -> Step — the only event producer
  ports/             # Protocols over kernel types; no logic
    provider.py      #   Provider: complete(req, on_delta) + info(model)
    tool.py          #   Tool, ToolSpec, Effects flags, ToolCtx, Workspace
    asker.py         #   Asker: resolve Ask verdicts (REPL prompts; oneshot policies)
    store.py         #   EventStore: append / replay / subscribe
  policy/            # pure functions; import kernel only
    context.py       #   window selection (recency + action-log), pressure tiers, compaction decision
    prompt.py        #   section thunks, content-hash memoization, stability tags
    guard.py         #   judge(call, effects, ws_root) -> Allow | Deny(reason) | Ask(question); chain
  drive/             # the async shell — the ONLY place asyncio appears outside adapters
    driver.py        #   executes Effects; TaskGroup per turn; READ-only batches parallel
    executor.py      #   ToolExecutor: schema validation, format:"path" containment, caps, sanitize
    retry.py         #   backoff decorator around the Provider port — the one retry surface
    bus.py           #   per-subscriber bounded queues; durable events -> store, all events -> sinks
    session.py       #   SessionHandle: open/resume/submit/subscribe/answer_permission/close
  adapters/
    anthropic.py     #   SDK, block buffering, string-aware JSON repair, cache placement, OAuth — quarantined
    tools/           #   bash, fs (read/write/edit), search (grep/find); workspace.py; proc.py (pgroup kill)
    mcp/             #   manager owns connect AND teardown; annotations -> Effects; env allowlist
    jsonl_store.py   #   fsync'd append, redact-before-append, snapshots, sidecar session index
  testing/           # EXPORTED public package: FakeProvider, MemoryStore, provider/tool conformance kits
  front/
    wiring.py        #   composition root: one frozen Settings; ALL os.environ reads live here
    repl.py          #   prompt_toolkit shell — a renderer + Asker over SessionHandle
    oneshot.py       #   forge run -p ... --json; emits run.json telemetry from TurnFinished
    render.py        #   Renderer object (own buffer, no module globals)
    commands.py      #   declarative slash commands (name, args, handler) shared by shells
```

**Import law** (enforced by import-linter in CI, not prose): `kernel` imports nothing internal; `ports`/`policy` import `kernel`; `drive` imports kernel+ports+policy; `adapters` import ports+kernel; `front` imports everything; `testing` imports ports+kernel. One import path per name — no re-export aliases, ever. The driver gets a line budget (~200) and a written razor: any branch keyed on tool names, provider names, or event contents is policy and belongs in `policy/` or the kernel.

---

## 3. Core abstractions

### 3.1 The kernel transition (most load-bearing)

```python
# kernel/step.py — pure, synchronous
@dataclass(frozen=True)
class CallModel:  request: ModelRequest            # request.purpose: "turn" | "compaction"
@dataclass(frozen=True)
class RunTools:   calls: tuple[ToolCall, ...]      # one batch; driver schedules by Effects
@dataclass(frozen=True)
class AskUser:    question: PermissionQuestion     # guard chain returned Ask
@dataclass(frozen=True)
class Finish:     result: TurnResult               # one terminal shape: ok|aborted|max_turns|fatal
Effect = CallModel | RunTools | AskUser | Finish

Input = UserInput | ModelOutput | ToolOutcome | PermissionAnswer | Cancelled

@dataclass(frozen=True)
class Step:
    state: SessionState
    effects: tuple[Effect, ...]
    events: tuple[Event, ...]      # the SAME Event type that hits disk, terminal, telemetry

def step(state: SessionState, inp: Input, policy: Policy) -> Step: ...
```

The kernel owns conversation-validity invariants and nothing else: matched `tool_use`/`tool_result` pairs, placeholder results on cancel, max-turns, result-size caps, permission bookkeeping, and the compaction transition (pressure the context policy can't relieve by truncation ⇒ `CallModel(purpose="compaction")`; the summary re-enters as an `Input`; a `Compacted` event carrying the summary lands in the log — so resume preserves it for free). Retry timing, rendering, persistence are *not here*. Target <500 lines, tested as `assert step(s, i, p) == expected` — no mocks, no asyncio. Two load-bearing property tests, treated as CI gates: `fold(replay(log)) == live_state` after every scenario, and every reachable state has matched tool pairs.

### 3.2 The Provider port

```python
# ports/provider.py
class Provider(Protocol):
    async def complete(
        self, req: ModelRequest,
        on_delta: Callable[[Delta], None] | None = None,   # render-only; never enters state
    ) -> ModelOutput: ...
    async def info(self, model: str) -> ModelInfo: ...      # replaces the static MODELS table

@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: tuple[PromptSection, ...]   # each tagged stability: STATIC|SESSION|VOLATILE
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    effort: Effort                      # NONE|LOW|MED|HIGH — adapter maps to budgets or drops
    purpose: Literal["turn", "compaction"]

@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_tokens: int
    pricing: Pricing | None             # None => report tokens, omit cost — never silently wrong
    efforts: frozenset[Effort]
```

One completion method. The Anthropic adapter internally buffers block deltas, owns string-aware streaming-JSON repair, maps stability tags to `cache_control` breakpoints, and quarantines the OAuth impersonation path. It takes no `cwd`/`project_root` — cache-TTL policy is a `Settings` field set in `wiring.py`. Adapters never raise for transient faults; `drive/retry.py` wraps the port — **one** retry surface, replacing today's two. The conformance kit in `forge.testing` (never-raise-transient, complete-tool-calls, usage-on-complete) is what a second adapter passes before it exists.

### 3.3 Tools, Effects, Workspace

```python
# ports/tool.py
class Effects(Flag):
    READ_PATH = auto(); WRITE_PATH = auto(); EXEC = auto()
    NETWORK = auto();   EXTERNAL = auto()    # EXTERNAL => default verdict is Ask

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: Mapping[str, Any]   # JSON Schema; fields marked format:"path" get contained
    effects: Effects            # drives BOTH the parallel scheduler and the guard chain

class Tool(Protocol):
    spec: ToolSpec
    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult: ...  # never raises

@dataclass(frozen=True)
class ToolCtx:
    ws: Workspace               # the ONLY path authority: ws.resolve(p) raises outside root
    cancel: asyncio.Event
```

Containment is belt-and-suspenders, fixing the 3-of-6 sandbox at two levels: the **executor** resolves every `format:"path"` schema field through the injected `Workspace` *before* `run()` (so forgetting is impossible, including for third-party tools), and the six built-ins also route all I/O through `ws` internally. Schema validation happens once, in the executor — the six hand-rolled `args.get` dances die. Subprocesses go through one seam with `start_new_session` + `killpg`, so aborts reap pipelines, not just direct children. Per-sub-agent workspaces fall out as constructor injection.

### 3.4 Event + Envelope + EventStore

```python
@dataclass(frozen=True)
class Envelope:
    seq: int; sid: str; parent: str | None   # parent: sub-agent provision, nothing more
    ts: float; v: int                        # per-body-kind schema version
    durable: bool                            # False = bus-only (deltas); True = fsync'd
    body: Event
```

Durable events: `UserSubmitted | TurnStarted | AssistantBlock | ToolDeclared | ToolStarted | ToolFinished | PermissionAsked | PermissionDecided(source=user|policy, reason) | Compacted(summary, first_kept_seq) | RetryScheduled | TurnFinished(outcome, usage, cost) | ChildSpawned | SessionEnded`. Transient: `TextDelta | ThinkingDelta | ToolOutputChunk` — live render only, superseded by the durable block-final event. Permission decisions are logged (Ledger graft, all three judges): audit and replay of every human/policy verdict come free for two event types. Store: fsync'd JSONL under `~/.agent-forge/sessions/`, redact-and-rewrite **before** append, snapshots every N turns keyed `(fold_version, seq)` and dropped if stale, sidecar index updated on append (O(1) session listing). `forge replay <sid> --until <seq>` reconstructs any state for free once `fold` exists.

### 3.5 Guard chain + Asker

`judge(call, effects, ws_root) -> Allow | Deny(reason) | Ask(question)` — pure predicates composed as an ordered chain; first Deny wins, all guards still observe (audit semantics preserved). `Ask` flows through the kernel as an effect, resolves via the `Asker` port (REPL prompts the human; oneshot applies a policy file), and both the question and the answer are logged events. Guards key on `Effects` flags and namespaced ids — never on tool-name strings, so renaming a tool can no longer silently disarm its guard.

### 3.6 SessionHandle

`open / resume / submit(text) / subscribe() -> AsyncIterator[Envelope] / answer_permission(id, allow) / close()`. All turn choreography — persist-before-run ordering, resume seeding, cost accounting, model swaps — lives here, below the UI line (Thin-Waist graft, Judge 1's anti-god-module fix). The REPL holds zero conversation state; a TUI or websocket front-end is a new subscriber, not a rewrite.

### 3.7 Context + prompt policies (pure)

`policy/context.py` selects the window per call (recency + action-log one-liners) and reports pressure tiers; token estimates calibrate against `Usage` from logged `ModelOutput`s — token sync is a fold input, not a scattered invariant. `policy/prompt.py` holds section *thunks* re-resolved at every build with content-hash memoization — `/clear` and `/remember` actually refresh, and the capture-by-value bug class is structurally gone.

---

## 4. One full turn

1. REPL: `session.submit("fix the bug")`. `UserSubmitted` is appended and fsync'd **before** anything else happens — crash-safety at the earliest moment.
2. Driver: `step(state, UserInput, policy)` → `CallModel(req)`. Context policy purely selected the window; prompt policy rebuilt sections (content-hash memoized); stability tags ride on each section.
3. Driver calls `retrying(provider).complete(req, on_delta=bus.transient)`. Deltas stream to the renderer (thinking dim, text buffered for one Markdown flush) as `durable=False` envelopes. A transient-fault retry re-renders but never half-mutates state; `RetryScheduled` is logged.
4. Completed `ModelOutput` → `step` → durable `AssistantBlock` + `ToolDeclared` events, and either `Finish` or `RunTools(batch)` (possibly preceded by `AskUser` effects where the guard chain returned Ask).
5. For `AskUser`: `PermissionAsked` is logged, the Asker resolves it (REPL prompt / oneshot policy), the answer re-enters as `PermissionAnswer`, and `PermissionDecided` is logged. Only that call pauses.
6. Driver executes the batch in one `TaskGroup`: calls whose combined `Effects` are read-only run concurrently; anything `WRITE_PATH|EXEC|EXTERNAL` serializes. The executor validates args, resolves path fields through `Workspace`, caps output (one knob), sanitizes errors. Outcomes re-enter as `ToolOutcome` inputs **in call-index order** — deterministic replay under concurrency.
7. Cancel: one `asyncio.Event`; the driver injects `Cancelled`, the kernel does placeholder-result bookkeeping purely, adapters translate `CancelledError` into well-formed terminal events. Subprocess kill is by process group.
8. Loop to step 2 (`CallModel` again) until `Finish`. If pressure crossed the compaction tier, the kernel emitted `CallModel(purpose="compaction")` and a `Compacted` event through the same pipe — one producer, no fabrication.
9. `TurnFinished(outcome, usage, cost)` — exactly one terminal shape. Renderer prints the footer from it; the persister already logged it; `oneshot.py` emits it as `run.json` / `--json`, which is the eval contract.

State ownership: the log owns truth; `SessionState` in the driver is `fold(log)` (resume and live are the same function — no duplicate message list anywhere); the context window is derived per-call by pure policy; config is one frozen `Settings` from `wiring.py` with zero `os.environ` reads below `front/`.

---

## 5. Key decisions

| Decision | Choice | Rationale | Replaces |
|---|---|---|---|
| Orchestration core | Pure `step(state, input, policy)`; dumb driver executes a closed Effect union | Table-testable without mocks/asyncio; god-function pressure has nowhere legal to land | `agent_loop`'s ~150-line generator + three lockstep accumulators; runtime/chat choreography split |
| Streaming | Deltas are render-only; kernel consumes one completed `ModelOutput` | Deletes the Anthropic block dialect and the partial-retry dedup hole in one move | 7-event `StreamEvent` dialect, `_stream_one_turn`, re-streamed UI content |
| Event production | Kernel is the sole producer; compaction is a kernel transition logging its summary | Phantom-event class structurally unrepresentable; summaries survive resume | `runtime.py:186` fabricated `CompactionAgentEvent`; dead 3-module compaction pipeline |
| Persistence | fsync-per-durable-event JSONL; state = `fold(log)`; snapshots `(fold_version, seq)` | Per-event crash safety; resume == live; self-invalidating snapshots | Turn-batched appends, chat.py's duplicate list, hand-rolled third message schema |
| Provider seam | `complete()` + `info()`; stability tags; effort enum; retry as a port decorator | n=1 honesty with a contract kit defining adapter #2; one retry surface | 7-event Protocol, double retry, static `MODELS` table, substring capability sniffing |
| Provider policy | All cache/OAuth/header/TTL policy inside the adapter or `Settings` | Transport adapters must not probe repo layout | `.agent-forge/` filesystem probe in the constructor; env reads deep in `_do_stream` |
| Sandbox | `Workspace` authority + executor-level containment on `format:"path"` fields | Uniform 6/6; forgetting is impossible, including for third-party tools | `_sandbox` applied to 3 of 6 tools; `os.path.join(cwd, '/etc')` escapes |
| Tool metadata | `Effects` flag enum; MCP `readOnlyHint`/`destructiveHint` mapped in; unannotated ⇒ `WRITE\|EXEC` | One declaration drives scheduler + guards; safe MCP defaults | Name-string guards (`call.name != "Bash"`), no parallelism |
| Permissions | `Allow \| Deny \| Ask` guard chain; `Asker` port; questions and answers logged | Interactive prompting is composition, not protocol surgery; audit free | Block-only `HookDecision`, private `_CompositeHook` |
| Hooks | Dissolved: observers = bus subscribers; gates = guard chain; rewrites/redaction before append | Disk, wire, and model always agree; convention becomes structure | Tiered transient/persisted hook semantics enforced by docstring |
| Event delivery | Bounded-queue `AsyncIterator` per subscriber; `Renderer` is an object | Slow renderer can't stall the loop; TUI/websocket/sub-agent render possible | Sync `on_event` callback; module-level `_text_buffer` |
| Prompt assembly | Thunks resolved every build, content-hash memoized, stability-tagged | `/clear` and `/remember` actually work; cache placement is an adapter mapping | Capture-by-value lambdas defeating section invalidation; Anthropic cache groups in the enum |
| Concurrency | asyncio confined to `drive/` + `adapters/`; `TaskGroup` per turn; READ-only parallel | 90% of code tests synchronously; parallelism is driver policy | Inline serial awaits; per-tool abort granularity; direct-child-only kill |
| Telemetry | `run.json` / `--json` from `TurnFinished`; golden-JSONL contract test shared with eval's parser | Eval and core cannot silently diverge | `parse_run.py` regex-scraping the ANSI footer |
| Architecture enforcement | import-linter contracts; concept index **generated** from code; CI link-lint | The manual doc tax was demonstrably skipped; automate the mirror | 675-line hand-maintained AGENTS.md, already drifted |
| Sub-agents | `sid`/`parent` envelope fields + `ChildSpawned` event — nothing else | Two fields now buy the child-stream model later | Nothing (and nothing more now) |

---

## 6. Where wiki, eval, MCP, and hooks live

**Wiki — a separate program, finally honest.** It reads two published, versioned surfaces: the repo itself, and session event logs (`kernel/events.py` schemas, the same serde the store uses). It writes `.agent-forge/wiki/*.md`; core integration is one prompt-policy thunk that includes a byte-budgeted file if present. No imports cross the boundary in either direction — the extraction failed last time precisely because the skill reached into `agent_forge.messages/provider` internals and broke on the package rename; the events schema plus `forge.testing` is the SDK surface it gets, and its CI runs against it.

**Eval — external, contract-pinned.** It drives `forge run -p ... --json` and folds `TurnFinished`/`ToolFinished` events for every metric (turns, tokens, cost, cache, wall time). A contract test in core's suite runs eval's parser against the kernel's golden event logs, so a renderer redesign can never null the metrics again — the parse_run.py failure mode becomes a failing test, not a silent zero. The two diverged methodologies (eval/ vs the eval skill) merge onto this one contract; host paths (`JAVA_HOME`, venv pytest) move out of task specs into harness env.

**MCP — an adapter that manufactures `Tool`s.** The manager owns connect *and* teardown (no more factory/runtime split lifecycle), namespaces `{server}__{tool}`, maps server annotations into `Effects` (unannotated ⇒ `WRITE|EXEC|EXTERNAL`, which defaults to Ask — we do not trust self-description with parallelism or auto-allow), scrubs child env to an allowlist instead of inheriting all host secrets, and threads the cancel event into call timeouts. TOML/CLI config parsing moves to `front/wiring.py` — deployment concerns leave the protocol module. Hot-reload is reconnect of one server; the registry's `_mcp_names` bookkeeping leak disappears because the manager, not the registry, tracks provenance.

**Hooks — dissolved into three honest mechanisms.** Observation (audit, metrics) = bus subscribers with independent cursors. Gating = the pure guard chain with logged decisions. Mutation = rewrite-before-append in the store path (tool-result redaction) and request middleware on the Provider port (wire-only rewrites, which are natural now because the wire view is a derived projection, never logged). The general-purpose Hook protocol with documented-but-unenforced persistence semantics does not survive; each of its three jobs lands where the structure enforces its semantics.

---

## 7. What we deliberately do not build

- **No MCP-for-builtins.** Native `Tool` protocol with in-process calls; the hot path (dozens of Reads per turn) pays no JSON framing, and per-call `Workspace`/cancel injection survives.
- **No plugin SDK, skill manifests, or semver'd ecosystem machinery.** Zero external plugin authors exist. The published surfaces are exactly two: the event schema and `forge.testing`. If an ecosystem materializes, the conformance kits are the seed.
- **No second provider adapter.** The contract kit defines what one must satisfy; we don't write speculative OpenAI code against an n=1 abstraction. `Effort` and stability tags are the documented guesses; the kit is where they get falsified cheaply.
- **No sub-agent implementation.** Only the `parent` field and `ChildSpawned` event. Building the spawn/coordinate/render machinery now would be designing against zero use cases.
- **No partial-text persistence.** Abort mid-stream discards undelivered assistant text by design; a UI that wants half-finished answers is out of scope.
- **No bash sandboxing claim.** `Workspace` contains path-taking tools; Bash gets `ws.root` as cwd, process-group kill, and the guard chain — heuristics, honestly labeled the floor. No proposal on the table solved this; we document the hole instead of pretending a regex closes it.
- **No TUI/web front-end** — but the async-iterator subscription means building one later is a renderer, not a rewrite.
- **No schema-migration framework.** Per-kind `v` plus a small dict of up-converter functions. If that ever hurts, the logs will tell us before a framework would.
- **No hand-maintained 675-line AGENTS.md.** A short prose intent doc plus a generated concept index plus import-linter. The DAG is a failing test, not a stale diagram.

---

## 8. Migration sketch (strangler order, each step shippable)

1. **Telemetry and log contract first.** Add `--json`/`run.json` to one-shot and per-event flushed JSONL (the `Envelope` shape) alongside the existing session format in chat.py; port eval's parser to it and add the golden-JSONL contract test. Lowest risk, kills footer-scraping immediately, and forces the event vocabulary to be designed against real consumers before anything depends on it.
2. **Collapse the provider seam.** Rewrite `LLMProvider` to `complete(req, on_delta) + info()`; the Anthropic adapter buffers its own block lifecycle internally. Move cache/TTL/credential policy out of the adapter into the composition root; delete the 7-event dialect, the double retry path (retry becomes a decorator), and the static `MODELS` table. The existing renderer keeps working off the delta callback.
3. **Extract the kernel.** Convert `agent_loop` into pure `step()` with `runtime.py` becoming the driver that executes effects. Compaction becomes a kernel transition; delete the fabricated `CompactionAgentEvent` and the dead `CompactionPort`/`append_compaction` pipeline. Land the two property tests (`fold == live`, matched tool pairs) and the import-linter contract in the same PR — they are the regression net for everything after.
4. **Workspace + executor.** Introduce `Workspace` and route all six tools through it; centralize schema validation, output caps, and sanitization in the executor; add `Effects` flags to `ToolSpec` and process-group kill to the subprocess seam. Convert guards to the `judge` chain with `Ask` wired to a REPL prompt; start logging `PermissionAsked/Decided`.
5. **SessionHandle.** Move chat.py's choreography (persist ordering, resume, cost accounting, slash-command dispatch) into `drive/session.py`; make state `fold(log)`, delete the duplicate message list, switch resume to replay + snapshots, add the sidecar index. chat.py shrinks to a renderer + Asker + declarative commands.
6. **Externalize wiki and eval; automate the docs.** Repoint the wiki skill at the now-public event schema and `forge.testing` (fixing its broken imports as a side effect); merge the two eval methodologies onto `--json`; replace AGENTS.md's hand-mirrored sections with the generated concept index + link lint. Delete every back-compat re-export alias in the same PR — one import path per name, enforced from then on.

Steps 1–2 are a week each against the existing test suite; step 3 is the risky one and is exactly where the property tests pay for themselves; steps 4–6 are independent once 3 lands.