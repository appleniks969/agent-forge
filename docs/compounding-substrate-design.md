---
title: Compounding Substrate — Ideal Core Design
status: proposal
date: 2026-06-13
provenance: multi-agent design panel (recon -> 3 architectures -> 3 adversarial judges -> synthesis); judges unanimously ranked Generalize-In-Place first, Ledger last
verified_against_tree: 2026-06-13
---

> Design note for generalizing the agent-forge-wiki engine into a source-agnostic
> compounding-loop substrate (Jira / meetings / code-review) and connecting it to agent-forge v2.

# Ideal Core Design: A Source-Agnostic Compounding Substrate

## 1. Direct answer

**Build one thing: a source-agnostic `Gatherer/Source` port that emits a schema'd, trust-tagged `RawItem` into the *existing* `agent-forge-wiki` `gather → compile → present → maintain` loop — and make the wiki's `proposed → validated → deprecated` entry lifecycle executable in code rather than prose.** This is the "Generalize-In-Place" spine grafted with `libcompile`'s two best ideas: a real `Trust` ladder wired into compile-time ranking (replacing the dead `Source` enum that `compile/bundle.py` reads nowhere), and an executable `Entry` lifecycle with cite-or-omit enforced at write time. Do **not** build Ledger's second append-only log — forge already *is* that substrate, and a parallel `~/.ledger/` event store is exactly the over-engineering the razor exists to prevent.

**Connect to agent-forge: YES — but only as a separate program reading published surfaces, never inside the kernel.** This is the literal application of forge's razor (DESIGN.md §1: *"if a feature can be a separate program reading the log, it is not in the core"*) and §6 (*"Wiki — a separate program… reads two published, versioned surfaces… No imports cross the boundary in either direction"*). The engine reads forge's published event-log JSONL via the `kernel/events.py` serde only; forge reads back exactly one byte-budgeted rendered file through the single sanctioned prompt-policy thunk. The prior extraction broke precisely because the skill reached into `agent_forge.messages/provider/models` internals — so the first move, before any generalization, is to sever that coupling (the violation is live today in `_llm.py:23-27`, `compile/runner.py:30-31`, `compact/runner.py:25-26`).

---

## 2. The core model — the source-agnostic substrate

The substrate is the unit of exchange, the pipeline that compiles it, and the cited wiki it produces. None of these are new abstractions; they are the *de-gitted* generalizations of what `scripts/wiki/types.py`, `storage.py`, and `compile/` already ship.

### The Source port (de-gitted `Gatherer`)

Today's contract hardwires git: `gather(self, repo_root: Path, since: datetime, cursor: dict)` (`types.py:97`). Three frozen assumptions break heterogeneity — `repo_root: Path`, a temporal `since`, and file-path-only area routing. Replace the input with an opaque handle:

```python
class Source(Protocol):                 # ports/source.py — Protocol over engine types only
    id: str                             # stable namespace: "jira", "meetings", "review", "sessions"
    runs_after: tuple[str, ...]
    timeout_seconds: int
    async def pull(self, h: SourceHandle, cur: Cursor) -> list[RawItem]: ...

@frozen
class SourceHandle:                      # the generalization of repo_root: Path
    kind: Literal["fs", "http", "log", "blob"]
    uri: str                            # "file:///repo" | "https://co.atlassian.net" | "forgelog://~/.forge/sessions/"
    area_router: AreaRouter             # by_path AND by_label/by_component/by_attendee

@frozen
class Cursor:                            # opaque per-source checkpoint — kills the `since` assumption
    token: str | None                   # jira changelog token, transcript batch id, last git sha, forge seq
    ts: datetime | None
```

`RepoHandle` semantics are preserved by `kind="fs"`, so every existing builtin keeps working byte-for-byte. The async runner (today's `gather/discovery.py`) is unchanged in spirit: auto-discover, topo-sort (Kahn, cycle-tolerant), per-source `asyncio.wait_for` timeout, per-failure `try/except` isolation, SHA-dedup, persist cursor. Registration stays *zero-ceremony*: drop a `.py` into `.agent-forge/sources/`, `_load_user_gatherers` imports it.

### The schema'd bundle: raw → derived → curated

This is the three-tier separation the recon found is *implied but not enforced*. Make it concrete:

```python
@frozen
class RawItem:                          # the renamed/extended Artifact — the ONLY thing crossing a source boundary
    id: str                             # STABLE, source-namespaced dedup key: "jira:PAY-1421", "pr:repo#423", "meet:2026-06-13#turn7"
    kind: str                           # opaque to the engine except for routing
    source_id: str                      # which adapter produced it
    trust: Trust                        # HARD | FIRM | SOFT | HUMAN — REPLACES the dead 4-value Source enum
    title: str
    body: str
    ts: datetime
    area: str | None
    refs: list[Ref]                     # typed cross-refs: Ref(kind="file"|"issue"|"incident"|"person"|"component", value)
    signals: dict[str, Any]             # open-ended bag, unchanged; carries _keep[] of novel signal keys to surface
```

- **RAW** — immutable, SHA-deduped on `id` (`storage.sha_seen/sha_record`, atomic tmp+`os.replace`). One JSON file per item under `.agent-forge/raw/cache/<kind-dir>/<safe-id>.json`. The sacred human/agent copy (transcripts, notes) mirrors verbatim to `raw/notes/`, which survives `rm -rf raw/cache/`. Re-running a source re-emits identical `id`s → skipped → cheap idempotent re-runs.
- **DERIVED** — a pure fold of raw into `Entry` rows, byte-budgeted and pre-ranked for the LLM. This is where cite-or-omit and the lifecycle live (below). Today this is only the *compile-time* bundle (`compile/bundle.py:build_compile_bundle()`); we promote it to a persisted, queryable layer (`entries/<area>.jsonl`).
- **CURATED** — the LLM-compiled narratives under `curated/*.md` (onboarding / hotspots / adrs / per-area), stamped `<!-- compiled: <ts> by wiki/compile -->`.

### The compiler

The single LLM stage stays the single LLM stage. `build_compile_bundle()` pre-ranks and pre-truncates `Entry` rows into one bounded JSON dict; `compile/runner.py` + the per-kind prompt cards in `assets/skills/<kind>.md` (with per-repo override resolution) turn it into curated markdown; `present/runner.py` mechanically renders (no LLM) into the byte-budgeted surface; `compact/runner.py` lints monthly; `maintain/runner.py` re-gathers stale areas. **Two surgical changes** to open the closed git vocabulary the recon flagged:

1. Add a generic `by_kind` roll-up section so a novel `kind` reaches the LLM at all (today `bundle.py`'s sections are hardcoded `hotspots/recent_commits/prs/adrs/notes/markers/session_insights`).
2. Make the kept-signal set `_KEEP_SIGNAL_KEYS ∪ (signals.get("_keep") or [])` — today `_KEEP_SIGNAL_KEYS` (`bundle.py:135`) is a closed allowlist that *silently drops* any signal it doesn't recognize, so a new source's facts never reach the LLM.

And **wire `Trust` into ranking** — today the `Source` enum (`types.py:24-29`) is read *nowhere* in compile, so the LLM gets zero provenance-based ordering. A reverted-PR fact (`HARD`) must always outrank a meeting line (`HUMAN`) on the same page.

### The entry lifecycle (executable, not prose)

This is `libcompile`'s decisive graft and the panel's strongest cross-pollination. Today the lifecycle is *prose only*: `write_wiki.py:54` hardcodes `state: proposed` as free text inside a bullet string, and `wiki_ops.py` has only `check-size`/`append` — no promote/demote, no citation tracking. Make it a real schema and a real state machine:

```python
@frozen
class Entry:                            # entries/<area>.jsonl — one JSON object per claim
    id: str
    area: str
    section: Literal["invariant", "rejection", "convention", "incident", "note"]
    claim: str
    state: Literal["proposed", "validated", "deprecated"]   # NOW a field, not a string
    citations: list[Cite]               # Cite(item_id, source_id, ts, quote) — cite-or-omit enforced at upsert
    first_seen_ts: datetime
    last_cited_ts: datetime
    cite_count: int
    contradiction_count: int
    trust_floor: Trust                  # min trust across citations
    published_eligible: bool            # Ledger's structural bar: fuzzy can't reach curated/ until promoted
```

**Anti-wiki-rot mechanisms** (the discipline ratchet already ratified, now made mechanical):

- **Cite-or-omit is a write-time invariant.** `WikiStore.upsert_entry` *rejects* any entry with empty `citations`. A claim with no source cannot exist.
- **`proposed → validated → deprecated` runs in code.** `lifecycle.observe(entry, new_citation | contradiction)` executes ratchet's rule: *"validated == cited in a second source without contradiction"* — and the second citation must come from a **different `source_id`** at `≥` the first's trust. A `HUMAN`/`SOFT` claim can never self-promote.
- **`published_eligible` is a structural gate** (Ledger's best idea, adopted without its substrate): fuzzy entries are *physically barred* from `curated/<area>.md` until promotion. Discipline becomes structure.
- **Staleness as a fold.** The per-area lag metric (`metrics/runner.py: staleness.json`) generalizes to `max(item.ts in area) − max(compile_ts in area)`; `maintain/` re-gathers stale areas.
- **Under-inclusion bias** ("when in doubt, do not ratchet") + `MIN_CONFIDENCE` write gate (`bootstrap/write_wiki.py:26`) survive as constants. Only `compact/` may delete, and only by demote/merge, preserving citations.
- **The three ungameable signals** (citation rate, override rate, staleness lag) measure the *compiled* layer, not where raw came from — already source-agnostic.

---

## 3. Source adapters — three sources, one interface

All three implement the identical `Source.pull()` contract; only `fetch` and the routing key differ. The honesty metric is a per-source position on the **crisp → fuzzy** spectrum, carried as `Trust`, and the human stays in the loop in inverse proportion to crispness.

### (1) Jira dump — `SourceHandle(kind="http")`, **Trust = FIRM** (crisp fields) / **SOFT** (free-text comments)

`pull()` walks the changelog from `cur.token`. Per issue:

```python
RawItem(
  id="jira:PAY-1421", kind="jira_issue", source_id="jira", trust=Trust.FIRM,
  title=summary, body=description + rendered_comments, ts=updated, area=None,
  refs=[Ref("component","payments"), Ref("issue","PAY-1419"), Ref("file","src/pay/webhook.py")],
  signals={"status":"Done","issuetype":"Bug","resolution":"Fixed","assignee":...,
           "_keep":["status","issuetype","resolution"]})
```

- **Honesty:** status/resolution/issue-type/links are machine-checkable and carry trust; a `resolution="Won't Fix"` is a crisp *rejection*. Free-text comments are `SOFT` and never auto-promote past `proposed`.
- **Routing:** `area_router.by_component(refs)` against a new `components:` map in `contexts.yaml` — **no file path needed**, fixing the "epic about a decision gets `area=None` and is silently dropped" gap (`bundle.py:109`).
- **Human-in-the-loop:** mandatory first-ingest validation session (the bootstrap discipline). Comment threads run the existing `classify → score → dedupe` chain (`bootstrap/classify_comments.py` taxonomy is already source-neutral).
- **Cursor:** `Cursor(token=next_changelog_token)`.

### (2) Meeting transcript — `SourceHandle(kind="blob")`, **Trust = HUMAN** (fuzziest)

A transcript has **no ungameable signal** — ASR error, sarcasm, *"let's NOT do X"* mis-parsed, decisions reversed later in the same meeting. So `pull()` does **not** mint one big artifact; it emits one `RawItem` per extracted decision/action-item:

```python
RawItem(
  id="meet:2026-06-13-arch#turn7", kind="meeting_decision", source_id="meetings", trust=Trust.HUMAN,
  title="<one-line decision>", body="<quote + surrounding turns>", ts=meeting_ts, area=None,
  refs=[Ref("person","sara"), Ref("component","auth")],
  signals={"attendees":[...], "speaker":..., "decision_marker":True,
           "needs_human_confirm":True, "_keep":["attendees","speaker"]})
```

- **Honesty:** `Trust.HUMAN` is the fuzzy floor. These enter `state="proposed"` with `published_eligible=False` and are **structurally barred** from curated pages. The raw transcript mirrors verbatim to `raw/notes/` so every claim cites a timestamped line.
- **Routing:** `by_attendee` / `by_component` (detected, LLM-free keyword pass); unmatched → an explicit `uncategorized/` review queue, **never silently dropped** (Ledger's rule).
- **Human-in-the-loop — the gate is the whole point:** promotion requires *either* an appended `human_confirmed` event *or* corroboration from a crisp source (a PR or Jira ticket asserting the same claim flips it via the `+0.1/mention` corroboration path). A meeting alone cannot manufacture a validated invariant.

### (3) PR / code-review — `SourceHandle(kind="fs")` + GH API, **Trust = HARD** (reverted) / **FIRM** (merged)

This is *already* the builtin `PRsGatherer` + ratchet's `gather_diff_context.py`. It maps with near-zero new work — proving the contract was source-agnostic all along:

```python
RawItem(
  id="pr:repo#423", kind="pr", source_id="review", trust=Trust.HARD if reverted else Trust.FIRM,
  refs=[Ref("file", p) for p in diff_paths] + [Ref("issue","PAY-1421")],
  signals={"is_revert":..., "files_changed":[...], "approvals":..., "incident_kw":...})
```

- **Honesty — the crisp end:** a revert is a revert; `CHANGES_REQUESTED` is recorded; `files_changed` is exact. Claims compiled from PRs **auto-validate** with little human friction (ratchet's "cited in a second review without contradiction"). Override-rate is the guard: a human dismissing a flag is a recorded signal that demotes.
- **Routing:** `by_path` (today's `areas_for_paths`, unchanged).
- **Per-comment compounding:** review threads feed the classify→score→dedupe chain to emit `kind="review_claim"` entries — exactly ratchet's five ratchet criteria.

| Source | Crisp signal | Trust | Auto-promote? | Human-in-loop |
|---|---|---|---|---|
| PR / review | revert, CI, CHANGES_REQUESTED | HARD / FIRM | yes, on 2nd citation | override-rate demotion |
| Jira | status, resolution, issue-type | FIRM (fields) / SOFT (prose) | fields yes, prose no | first-ingest validation |
| Meeting | **none** | HUMAN | **never** alone | confirm event OR crisp corroboration |

---

## 4. How it connects to agent-forge

**Placement: entirely in the `front`/skill tier — same layer as wiki/eval/MCP/hooks in DESIGN.md §6. Never in `kernel`, `ports`, or `adapters`.** Forge's import law: `kernel` imports nothing internal; `ports` are Protocols over kernel types; `adapters` import ports+kernel; `front` imports everything. The engine is a separate program in `front`'s tier.

**Published surface the engine READS (and nothing more):**
- The git repo (`kind="fs"` handle) — for the existing builtins.
- **Forge's event-log JSONL** via the published `kernel/events.py` `Envelope` serde — the *same serde the store uses*, read-only. This is the new `SessionLogGatherer` / `kind="log"` handle, folding envelopes into `kind="session_insight"` RawItems. It finally wires the "sessions as a source" half of §6 that is designed-but-unbuilt — **as just another source, not a kernel change.**
- External source APIs/dumps (Jira, transcripts) behind `SourceHandle`.

**Published surface forge READS BACK (and nothing more):** the rendered bytes of `curated/<area>.md` via **one** prompt-policy thunk in `policy/prompt.py` that byte-budget-includes the file if present. Render-only. No import of the engine by forge.

**What must NOT import what (CI-enforced — this is the §6 lesson):**
- The engine must **not** import `agent_forge.messages` / `agent_forge.provider` / `agent_forge.models` internals. *This violation is live today* (`_llm.py:23-27`, `compile/runner.py:30-31`, `compact/runner.py:25-26`) — the exact coupling that broke the prior extraction on a package rename. The fix: define the engine's own one-method `LLMClient` port and drop `_llm.one_shot` to the `claude -p` subprocess that `bootstrap/classify_comments.py:69` already uses (no SDK, no `ANTHROPIC_API_KEY`). Then an `import-linter` contract (copied from forge's real `.importlinter` on disk) fails CI on any `import agent_forge.` inside the engine.
- `kernel`/`ports`/`adapters` must never import the engine.

**Reuse, don't rebuild:**
- **Reuse the `agent-forge-wiki` engine wholesale** — `gather/discovery.py` orchestration, `Artifact`/storage with SHA-dedup, `_noise.is_noisy_path`, the compile/compact/present/maintain stages and their prompt cards, the three metrics. The generalization is ~four localized edits, not a rewrite.
- **Reuse ratchet** — its classify→score→dedupe→write chain becomes the comment-stream→entry path for *any* source; its five ratchet criteria and under-inclusion bias become the promotion policy; its `proposed/validated/deprecated` semantics are what we make executable.

---

## 5. Why not the alternatives

Two trade-offs decided it.

**vs. `libcompile` (full package extraction) — rejected as the *starting move*, adopted as the *end-state discipline*.** `libcompile` has the cleanest end state and the most honest fuzzy handling (the `Trust` ladder and executable lifecycle — which I grafted). But it pays a full package carve — `types→ports→adapters→drive` split, an `Artifact→RawItem` schema migration over existing `raw/cache` JSON, a second LLM seam, and a published compatibility contract — *before any new source value lands*, and it touches all three repos at once. The razor rewards adding the *least*. So: ship `libcompile`'s discipline *incrementally inside the existing `scripts/wiki/` tree* (ports, import-linter, testing fakes, executable lifecycle) rather than as a big-bang extraction. Same destination, strangler path, repo never stops working.

**vs. Ledger (second event-log substrate) — rejected outright.** Ledger is the most elegant and has the highest compounding ceiling (corroboration-as-a-fold, time-travel replay). But it *inverts the razor's intent*: the razor says "if a feature can be a separate program reading the log, it is not in the core" — it does **not** say "rebuild forge's kernel inside the wiki." Forge **already is** an append-only-log-with-fold substrate (`jsonl_store.py`, `state.fold`, the CI-gated `fold(replay(log)) == live`). Building a parallel `~/.ledger/` log beside it means a dual-write migration window, single-writer serialization, snapshot machinery, never-forget redaction obligations for transcripts, and a brand-new claim-identity-by-normalized-text fold with no existing analogue — *for a knowledge base fed by one source today*. Maximum new machinery for a payoff that only materializes once many sources feed the same areas. We steal its two cheap, high-value ideas (corroboration as a derive-pass over the existing store; the `published_eligible` structural bar; "unrouted → review queue, never dropped") without its substrate.

---

## 6. Build sequence (strangler, each step shippable)

Starts from today's `agent-forge-wiki` gatherer engine; nothing stops working between steps.

- **Step 0 — Sever the import-law violation (ships alone, highest leverage).** Rewrite `_llm.one_shot` to call `claude -p` as a subprocess; delete the `agent_forge.messages/provider/models` imports from `_llm.py`, `compile/runner.py`, `compact/runner.py`. Add an `import-linter` contract (copied from forge's real `.importlinter`) that fails CI on `import agent_forge.` in the engine. Repo behaves identically; the §6 boundary now actually holds.
- **Step 1 — De-git the input.** Introduce `SourceHandle` (default `kind="fs"`) + `Cursor`; change `gather` → `pull(self, h: SourceHandle, cur: Cursor)`; discovery passes `fs` handles. Every builtin reads `h.uri` → byte-for-byte identical. All tests green.
- **Step 2 — De-git the routing + open the bundle.** Add typed `Ref`s and `AreaRouter` (`by_path` ∪ `by_component`/`by_attendee`); route `area=None` to an explicit `uncategorized/` queue. In `bundle.py`, add the generic `by_kind` roll-up and `_KEEP_SIGNAL_KEYS ∪ _keep`. Wire the `Trust` enum (replacing the dead `Source` enum) into compile ranking.
- **Step 3 — Make the lifecycle executable.** Add `entries/<area>.jsonl` + the `Entry` schema; port the prose lifecycle into `core/lifecycle.py` with cite-or-omit enforced at `upsert`, the different-`source_id` promotion rule, and the `published_eligible` bar. **Do this before any non-PR source** so entries cannot multiply faster than they converge.
- **Step 4 — First real source, crisp end (lowest risk, data already exists).** Feed `PRsGatherer.signals.inline_comments` through classify→score→dedupe to emit `kind="review_claim"`; add a `review_claims` card. Repoint ratchet's code-review skill to call the engine. Validates the whole widened path where the metric is trustworthy.
- **Step 5 — Make the classifier scale (P0 precondition).** Before any large dump: add batching + concurrency + checkpoint/resume to `bootstrap/classify_comments.py` (today serial, one subprocess/comment, ~$50/50k, no resume — blocks the *first* multi-source backfill, not a later optimization).
- **Step 6 — Jira source.** Drop `sources/jira.py` (`kind="http"`, `by_component` routing, crisp `Trust.FIRM` fields); add a jira card. Mandatory first-ingest validation session.
- **Step 7 — Meeting source (fuzzy end).** Drop `sources/meetings.py` (`kind="blob"`, `Trust.HUMAN`, transcript mirrored to `raw/notes/`); render in a distinct unconfirmed block, `published_eligible=False`, wire override-rate demotion and the `human_confirmed`/corroboration promotion path.
- **Step 8 — Wire forge sessions + close the loop.** Add `sources/sessions.py` (`kind="log"`) reading the forge event log via the published serde **only**, plus the one prompt-policy thunk feeding `curated/<area>.md` back to the agent. (Do this *last* — see risks for the path/format hazards.) The compounding loop is now closed end-to-end with no crossing imports.
- **Step 9 — Reconcile docs + content migration.** Write the missing `references/ARCHITECTURE.md` + `SCHEMAS.md` (today fictional), migrate the *existing* wiki content from both corpora (`agent-forge-wiki`'s `curated/` and ratchet's `area-wiki/<slug>.md`), and unify the two incompatible `contexts.yaml` dialects into one schema.

---

## 7. Honest risks — what stays hard

- **The forge session source is the single biggest unbudgeted hazard, on two axes the panel verified on disk.** (a) **Wrong path:** the new kernel store writes to `~/.forge/sessions/`, but every design assumed `~/.agent-forge/sessions/` (the *old* location) — a naive adapter finds no new envelopes. (b) **Mixed-schema graveyard:** the sessions directory holds ~150+ JSONL files, but the overwhelming majority are *legacy* `{"type":"metadata"/"message"}` (AnthropicProvider era) and only the newest handful are real `Envelope`s. A naive `*.jsonl` glob folds the legacy garbage into the wiki, or — reading via the envelope serde only — *silently drops the older ~60%+ of history*, the richest knowledge source half-eaten. **Mitigation:** a dual-format adapter that version-discriminates on the `Envelope.v` field, reads the correct path, and quarantines foreign-schema lines. Build it last; the engine delivers full value on git/jira/meetings without it.

- **Garbage-in from fuzzy sources is mitigated, not solved.** `published_eligible` + `Trust=HUMAN` + the different-`source_id` rule stop a *lone* transcript hallucination from compounding. They do **not** stop **causally-linked corroboration**: a meeting that *drove* a Jira ticket produces two non-independent sources echoing the same wrong claim, and any corroboration rule will promote it. **Mitigation:** record source *provenance lineage* (`Ref("issue", ...)` already links a meeting decision to the ticket it spawned) and treat a citation as independent only when the two items share no lineage edge. This is genuinely hard and stays a known false-positive path.

- **Cite-or-omit guarantees provenance, not correctness.** A confidently-wrong claim citing a real-but-misinterpreted transcript line *passes* cite-or-omit. The mechanism proves "we have a source," not "the source is right." Only crisp corroboration or human confirmation closes that gap.

- **Fuzzy metrics have no ground truth.** Meeting `Trust` is a judgment call; mis-tagging a `SOFT` source as `FIRM` re-opens auto-compounding. And the three signals only measure what *was* written — there is **no recall/coverage signal**, so a wiki that is 100% precise and 5% complete looks healthy on every metric. The compounding thesis can fail silently by accumulating *nothing useful*.

- **The self-tuning loop is unwired today and stays manual through the rollout.** The metrics recorders are write-only with no automated trigger from a high override/staleness counter to a re-gather or human nudge (`metrics.json` is hand-incremented). Adding 3+ sources multiplies the `proposed`/unconfirmed backlog while human attention stays manual — the fuzzy review queue risks becoming an un-triaged graveyard, exactly the wiki-rot the project exists to prevent. **Mitigation:** wire a high-counter → re-compile/notify trigger (defer-but-don't-forget).

- **Cross-source identity/entity resolution is the deep unsolved problem.** A person is `git: jdoe`, `jira: john.doe@`, `meeting: "John"`; a component is a file glob *and* a Jira component label *and* a spoken name. `Ref` typing helps but the resolution table (who/what is the same entity across git + Jira + meetings) is hand-maintained and will drift. This is where corroboration quietly fails — two items about the same thing never converge because their entity refs don't match.

- **Byte-budget contention at the final hop.** The compile bundle is one bounded JSON blob and `present/runner.py` injects one byte-budgeted section. With sessions + Jira + meetings + reviews all compiling into one area's page, the injection slot the agent actually sees is fixed while the payload grows unbounded. No design models per-source sub-budgets or an eviction/priority policy — the most important hop of the compounding loop is unsized at multi-source scale.

**Key files:** `/Users/nikhilsalunke/agent-forge/.claude/skills/agent-forge-wiki/scripts/wiki/types.py` (the `Gatherer`/`Artifact` contract to de-git), `.../scripts/wiki/gather/discovery.py` (the runner to generalize), `.../scripts/wiki/compile/bundle.py` (the closed git vocabulary at lines 109/135), `.../scripts/wiki/_llm.py` + `compile/runner.py` + `compact/runner.py` (the import-law violation to sever first), `/Users/nikhilsalunke/agent-forge/DESIGN.md` (§1 razor, §6 wiki boundary), `/Users/nikhilsalunke/ratchet/bootstrap/classify_comments.py` (the classifier to make resumable), `/Users/nikhilsalunke/ratchet/.claude/skills/code-review/scripts/wiki_ops.py` + `bootstrap/write_wiki.py` (the prose-only lifecycle to make executable).

---

## Verification notes (checked against the live tree, 2026-06-13)

- **Import-law violation — CONFIRMED LIVE.** `grep` finds `from agent_forge.messages/models/provider` in `scripts/wiki/_llm.py:23-27`, `compile/runner.py:30-31`, `compact/runner.py:25-26`. Step 0 (sever, then import-linter gate) is correct and is the highest-leverage first move.
- **Source-agnostic contract — CONFIRMED.** `scripts/wiki/types.py` already defines `Artifact(id, kind, source, title, body, ts, area, signals{})` and a `Gatherer` base with `gather(repo_root, since, cursor) -> list[Artifact]`, auto-discovered from `.agent-forge/gatherers/`. The only git-coupling to remove is the `repo_root: Path` + temporal `since` signature — exactly the SourceHandle/Cursor generalization above.
- **Sessions path — CORRECTION to §7.** The doc says the new store writes `~/.forge/sessions/` and designs assumed `~/.agent-forge/sessions/`. On disk it is the **reverse**: `jsonl_store.default_root()` returns `~/.forge/sessions/` (empty, 0 files), while the live composition root (`forge/front/wiring.py:73`, with an in-code comment noting the discrepancy) writes to `~/.agent-forge/sessions/` (**154 files**). The `sessions.py` adapter must read `settings.sessions_root`, never `default_root()`. The mixed-schema-graveyard hazard (legacy `{type: metadata|message}` vs new `Envelope`) still applies and should be version-discriminated on `Envelope.v`.
