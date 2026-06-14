---
title: Compile — Ideal-From-Scratch Compounding Substrate
status: proposal (clean-sheet / greenfield)
date: 2026-06-13
provenance: clean-sheet design panel (3 architectures: functional-core, knowledge-graph, small-sharp-tools -> 3 adversarial judges -> synthesis). Judges split 2-1 for functional-core; synthesis grafts all three.
companion: compounding-substrate-design.md (the generalize-in-place answer)
---

> Greenfield reference design. See the final section for what changes vs the generalize-in-place answer.

# Compile — The Ideal-From-Scratch Compounding Substrate

## 0. How I read the panel

Three architectures, three judge panels, near-total agreement on the *shape* of the truth:

- **Fold (functional-core)** wins **conceptual integrity and buildability** — "the KB is `fold(compile, EMPTY, log)`" generates regenerability, replay==resume, no-hidden-state, and retract-and-refold *as consequences*, not features. Two of three panels rank it first.
- **PGS (knowledge-graph)** wins **hard-problem coverage and ceiling** — it alone notices that "validated == cited by a different source" *is a graph predicate*, so entity-resolution and causal-independence must be load-bearing edges, not footnotes. Its `DERIVES`-edge-you-subtract and its **consequence-ranked under-merge queue** are the sharpest honesty mechanisms in the set.
- **Small Sharp Tools** wins **time-to-value and the boundary argument** — the prior wiki broke by reaching into agent internals and snapping on a rename, and a *process boundary makes that coupling physically impossible*, plus cite-or-omit as a `minItems:1` commit-gate is the most unforgeable encoding of an invariant.

Every panel reached the same verdict independently: **these are not three rival architectures, they are one architecture at three altitudes.** PGS is right about *what to reason over*, Fold about *how the engine is built*, Small Tools about *how to start and how to stay honest at the boundary*. I'm grafting accordingly, not crowning.

---

## 1. The one-paragraph verdict

**The knowledge base is a pure fold over an append-only fact log, and the thing it folds *into* is a typed provenance graph — because the one rule that does all the safety work ("a claim is validated when a *different* source cites it at >= trust with no contradiction") is a graph predicate, and a predicate over edges is only honest if the fold that computes it is pure, total, and replayable.** Concretely: there is exactly one authoritative artifact on disk — an `fsync`'d append-only JSONL fact log; `state = fold(compile, EMPTY, log)` is a pure, sans-IO, import-linter-sealed function whose output is a graph of content-addressed `Claim` nodes with `CITES / CONTRADICTS / DERIVES / ALIAS_OF` provenance edges; lifecycle, trust, entity-resolution and causal-independence are *edge-walks inside that fold*, never stored mutable rows; the LLM touches the system in exactly one stage, sandwiched by a pure validator on both sides, behind a swappable subprocess seam; and every invariant (cite-or-omit, trust-gating, purity, regenerability) is a CI gate that *fails the commit*, not a discipline that hopes to be remembered. You build it Small-Tools-first to earn the graph incrementally rather than erecting a framework on day one.

This is named **Compile** — it is a compiler, not a chatbot: it does the work once and the artifact compounds.

---

## 2. Module tree & the import law

I take PGS's data model, Fold's purity discipline, and make the boundary law enforceable the way all three panels demanded.

```
compile/
  core/                    # PURE. sans-IO, total. THE only place truth is computed.
    ids.py                 #   content-addressed ids (blake3) — dedup & replay-stability
    model.py               #   RawItem, Claim, Entity, Edge, Citation — frozen ADTs
    facts.py               #   closed Fact sum type (the log vocabulary)
    fold.py                #   fold(facts) -> Graph   ≡ functools.reduce(compile, facts, EMPTY)
    compile.py             #   compile(graph, fact) -> graph'   ← THE kernel, total match
    normalize.py           #   triple normalization (subject,predicate,object) — see §6, the crux
    resolve.py             #   entity resolution: blocking → score → 3-band merge (pure)
    lineage.py             #   DERIVES inference; subtract-before-counting (pure)
    independence.py        #   independent_sources() — causal-cluster collapse (pure)
    lifecycle.py           #   the ratchet: proposed/validated/deprecated (pure recompute)
    trust.py               #   HARD/SOFT/HUMAN lattice + promotion gate (pure)
    select.py              #   byte-budget page selection (pure, deterministic)
    queue.py               #   consequence-ranked review queue (pure)
    errors.py              #   a rejected fact is data, not an exception

  ports/                   # PURE protocols only (typing.Protocol). No logic, no impls.
    source.py              #   Source.pull(handle, cursor) -> list[RawItem]
    renderer.py            #   Renderer.render(RenderRequest) -> RenderResult  ← the LLM seam
    clock.py               #   Clock.now() -> Ts   (even time is a port)
    log_store.py           #   LogStore.append / read_all

  adapters/                # EFFECTFUL. implements ports/. imports ports + core TYPES only.
    sources/
      jira.py  meeting.py  pr.py  forge_session.py  fs_glob.py
    renderer_subprocess.py #   real LLM via stdin/stdout subprocess contract (§6)
    renderer_stub.py       #   deterministic, offline test double
    log_jsonl.py           #   fsync'd append-only JSONL
    clock_system.py

  policy/                  # PURE DATA. trust tables, resolver thresholds, edge weights.
    identity.fact.jsonl    #   human-seeded cross-vendor identity — AS FACTS, not a side file (§8)

  drive/                   # EFFECTFUL orchestration. the imperative shell.
    ingest.py  derive.py  render.py  project.py  maintain.py

  front/                   # EFFECTFUL entry points. CLI / skill / cron.
    cli.py                 #   compile ingest|derive|project|lint|review|replay

  spec/                    # THE FROZEN CONTRACT (small-tools graft)
    raw.schema.json  fact.schema.json  entry.schema.json  render.schema.json
    validate               #   `validate fact.schema.json < log/facts.jsonl`

  store/
    log/facts.jsonl        # THE SUBSTRATE. append-only. fsync'd. source of truth.
    snapshot/graph.db      # DERIVED cache (SQLite). regenerable. NEVER authoritative.
    out/wiki/*.md          # DERIVED projection. regenerable. never hand-edited.
    metrics.json  findings.jsonl
```

**The import law — three layers of enforcement, CI-gated:**

| Rule | Mechanism | What it buys |
|---|---|---|
| Layering `front → drive → {adapters, ports, core}`; `adapters → {ports, core-types}`; `core → core` | **import-linter** contracts (Fold/PGS) | lower layers never import higher |
| `core` imports no `io/os/time/random/datetime.now`/model SDK/`adapters` | import-linter **forbidden-modules** + an **AST lint** banning nondeterministic builtins in `core/` | replay & table-testing guaranteed *by construction* |
| `core` never names an agent type; the token `forge` appears in exactly one file (`adapters/sources/forge_session.py`) | import-linter contract | the rename that snapped the old skill is impossible |
| a `pure=true` adapter that opens a socket fails | **strace/syscall manifest check** (small-tools graft) | "deterministic except render" is *enforced*, not asserted |

The decisive graft from the panels: **import-linter alone is not enough.** Small-Tools' two checks — the schema commit-gate and the syscall purity check — turn purity and cite-or-omit from architectural promises into things you *cannot merge around*. Every panel independently recommended this transplant.

---

## 3. Data & storage model

### The shapes

Three the consumer already knows (`RawItem`, `Entry`/`Claim`, `Curated`), plus **`Fact`** (the only thing written) and **`Entity` + `Edge`** (because, per PGS, cross-source identity and provenance are *first-class graph objects*, not fields/strings). All frozen. All content-addressed.

```python
# RawItem — atomic immutable evidence. never edited; a "change" is a new :v2 item.
RawItem{ id, content_hash, kind, source_id, trust, title, body, ts, refs[], signals{} }

# Fact — the ONLY log record. closed sum type so compile() is a total match.
Fact =
  | Observed     {raw: RawItem}                              # a gatherer saw bytes
  | Claimed      {claim_id, triple, citation, trust}         # distilled claim, born proposed
  | Corroborated {claim_id, citation}                        # another source cited it
  | Contradicted {claim_id, citation, note}
  | Aliased      {entity_id, mention, weight, by}            # an entity-resolution edge
  | Split        {entity_id, into[], reason}                 # undo a bad merge by appending
  | Derived      {from_raw, to_raw, relation, by}            # a DERIVES edge (causal link)
  | Confirmed    {target, human, decision}                   # the ONE human-in-loop verb
  | Rendered     {page_id, claim_ids[], prose_hash}          # LLM output, subordinate
  | Retracted    {fact_ref, reason}                          # logical delete = append
  envelope: {fact_id: blake3(payload), ts, agent, prev}      # hash-chained, tamper-evident

# Claim (Entry) — DERIVED node. identity is MEANING, not provenance.
Claim{ id = blake3(normalize(subject,predicate,object)),     # ← two sources, same claim, same id
       subject: EntityRef, predicate, object,
       state, trust_floor }
       # citations/contradictions are EDGES into this node, not a field.

# Edge — provenance is FIRST-CLASS and gradeable (PGS's core insight)
Edge{ kind: CITES|CONTRADICTS|CONCERNS|ALIAS_OF|DERIVES|SUPERSEDES,
      src, dst, weight, by: "source_asserted"|"resolver:v3"|"human:nik"|"lineage:v2", ts }

# Entity — cross-source identity as a derived cluster
Entity{ key, kind, handles: frozenset[str], confidence }
```

**The load-bearing modeling decision (PGS, endorsed by every panel):** a `Claim`'s identity is its *normalized triple*, so two different sources asserting the same thing **converge on one node automatically** — corroboration becomes "count distinct `CITES` edges from different sources into this node," and dedup is a property of the type, not a fuzzy pipeline stage. Provenance hangs off as edges, which is what makes "show me every validated claim whose validation depended on a resolver guess below 0.9" a query, not a guess.

### Where truth lives & how reads work

- **Source of truth: `store/log/facts.jsonl`** — append-only, `fsync`'d, hash-chained, git-tracked. *That file is the database.* It is the only thing backed up.
- **The graph (`snapshot/graph.db`, SQLite) is a derived cache, not a source of truth.** This is the one place I diverge from PGS's framing and side with Fold: PGS's dual-store reintroduces the "cache disagrees with reality" seam Fold eliminates. **Resolution: the graph is `fold(log)` materialized purely as a query accelerator.** Anything in it not recomputable from the log + pinned resolver/lineage/normalizer versions is a *bug a CI gate catches* (rebuild twice, byte-compare). It earns its place only as an index, never as state.
- **Reads never hit the log for user queries.** Three read paths in increasing specificity (small-tools): *curated read* (`cat out/wiki/auth.md` — the 99% compounding path), *claim read* (graph query for structured claims + citations), *provenance read* (footnote → claim → `CITES` edge → `RawItem` body, verbatim, in two hops).
- **Regenerability is the headline.** `rm -rf store/snapshot store/out && compile project` reproduces every page byte-for-byte for the deterministic portions; `Rendered` prose is cached by `prose_hash` keyed on the exact claim set, so re-projecting is free unless claims actually moved.
- **Scaling to many sources:** add a source = one `Source.pull` adapter + one distiller + a `source_id` shard appended to the log. Zero core changes. Cross-source corroboration and entity resolution happen *in the fold over the unified stream* — which is exactly where a person who is `jira:john.doe@` + `git:jdoe` + meeting-`"John"` becomes one `Entity`.

---

## 4. The pipeline & the LLM seam

Five stages. **Exactly one is effectful-and-nondeterministic (render). One is effectful-but-deterministic (gather). Three are pure.**

| Stage | Layer | Pure? | LLM? | What it does |
|---|---|---|---|---|
| **gather** | drive+adapters | effectful, deterministic-given-bytes | no | `Source.pull(handle, cursor)` → `RawItem`s, wrapped as `Observed` facts, appended. Idempotent via `content_hash`. Cursor persisted *as a fact* → exact resume. |
| **derive** | **core** | **pure** | no | `fold` folds `Observed` → `Claimed/Corroborated/Contradicted/Aliased/Derived`. Normalize, resolve, lineage, trust, lifecycle — *all here, all edge-walks.* |
| **render** | adapters | effectful, **nondeterministic** | **YES — only here** | `Renderer.render(selected claims) -> prose`, appended as `Rendered`. Subordinate; cannot alter state. |
| **present** | drive | effectful, deterministic | no | `fold(log) → graph`, query → `out/wiki/*.md` + `metrics.json`. Pure function then a `write()`. |
| **maintain** | core+drive | pure core, effectful flush | no | compact (segment log; emit `Retracted` for dead facts) + lint (citation rot, contradiction sweep, coverage proxies). Lint *proposes facts*; never mutates. |

**The crucial inversion vs RAG:** derive runs **once per fact, not once per query**. Distillation and render are amortized into the log and projection. Next month's query is a `grep` over already-compiled `wiki/`. That is the compounding.

### The LLM seam — Fold's two-sided guard behind Small-Tools' subprocess boundary

The single best LLM-seam idea in the set is Fold's: a **pure validator on *both* sides** of the model. The single best *faithfulness* idea is Small-Tools': make the seam a **subprocess (stdin JSON → stdout markdown)** so the test double cannot diverge from the real one — there's no interface to mock wrong. I take both.

```python
# ports/renderer.py — PURE protocol
class RenderRequest(NamedTuple):
    page_id: str
    blocks: tuple[Block, ...]   # already SELECTED, budget-fitted, CITED claims w/ pre-assigned [^id]
    style: RenderStyle          # deterministic knobs (temp=0)

class RenderResult(NamedTuple):
    prose: str
    used_block_ids: tuple[str, ...]

class Renderer(Protocol):
    def render(self, req: RenderRequest) -> RenderResult: ...
```

- **Before:** the model receives *only* cited, budget-fitted claims with footnote tokens **pre-assigned by core**. It cannot cite something core didn't hand it.
- **After:** a pure `core/render_guard.py` rejects the render (`errors.RenderEscapedCitations`) unless every `[^id]` in the output ⊆ input blocks. On rejection the page keeps prior prose or falls back to flat templated rendering of the same claims — ugly but correct. **Cite-or-omit survives an adversarial/hallucinating model.**

**Test double** (`renderer_stub.py`) — set `RENDERER=./renderer_stub`, the whole pipeline is deterministic and offline:
```python
prose = "\n".join(f"- {b.subject} {b.predicate} {b.object} [^{b.id}]" for b in req.blocks)
```
99% of tests never touch a model; the real subprocess renderer is exercised by one small network-gated contract test asserting only that the guard holds and citations are closed-world — **never asserting wording.**

**Honest residual the panels flagged and I will not paper over:** the guard proves a sentence is *cited*, not that the prose *preserves the claim's meaning* (rendering "must back off" as "should back off"). Cite-or-omit is provenance, not correctness, *at the render layer too*. Mitigation, not solution: the stub is the ground truth for claim *content*; the model only rephrases, and the area page links each sentence to its claim node so a human reviewing a page can diff prose against the structured claim. I flag this as residual drift, not a closed problem.

---

## 5. Lifecycle, trust & anti-rot — as code-level rules

### Trust is a lattice, assigned by the gatherer from the source, never inferred
```python
class Trust(IntEnum):
    HUMAN = 1   # a meeting line — cannot self-promote
    SOFT  = 2   # a Jira field, a PR description
    HARD  = 3   # a revert, CI red, a merged diff — the world mechanically enforced it
```
Trust is a statement about *what kind of evidence exists*, not a quality score the LLM could game.

### The ratchet — state is RECOMPUTED by the fold, never stored (Fold + PGS)
```python
# core/lifecycle.py — total, pure, no I/O
def recompute_state(claim, edges, sources, lineage) -> ClaimState:
    contras = contradiction_edges(claim, edges)
    if contras and max_trust(contras) >= claim.trust_floor:
        return DEPRECATED                          # demote — KEEP all edges

    witnesses = independent_sources(cite_edges(claim, edges), sources, lineage)  # §6 causal collapse
    if len(witnesses) >= 2 and not contras and can_promote(claim, witnesses):
        return VALIDATED                           # the ratchet clicks forward
    return PROPOSED
```

### The two invariants as code-level rules

**Invariant 1 — cite-or-omit, enforced at THREE boundaries** (the panels wanted defense in depth here):
1. **Type:** `Claim.create` has no constructor accepting empty citations → an uncited claim is *unrepresentable*.
2. **Schema/commit:** `entry.schema.json` sets `citations: {minItems: 1}`; `spec/validate` fails the *commit* (small-tools graft) — the invariant is a property of the file, not code that might forget.
3. **Render:** the guard drops any unfootnoted sentence.

CI property: *for all claims in `fold(any log)`, `cite_edges(claim) >= 1`.*

**Invariant 2 — trust-gated promotion:**
```python
def can_promote(claim, witnesses) -> bool:
    if all(trust(w) == HUMAN for w in witnesses):
        return False                 # HUMAN-only can NEVER self-promote, regardless of repetition
    return True
# AND select.py structurally bars HUMAN-only claims from pages:
#   block ∈ page  iff  state==VALIDATED and (max_trust >= SOFT or has_human_confirmation)
```
A meeting line, however many times repeated *in meetings*, cannot reach a curated page until a crisp source corroborates or a human appends `Confirmed(promote)` — whose confirmation is itself a HARD `CITES` edge (`by="human:nik"`), so provenance is preserved and the meeting line stays attached as origin.

**Anti-rot:** demote/merge **never lose citations** (they *union/re-point* edges); a deprecated claim renders `~~X~~ (deprecated, contradicted by [^7])`. Garbage-in is bounded by **retract-then-refold** (Fold's sharpest property): append `Retracted`, re-fold, and *exactly the transitive descendants* of the bad fact vanish from the projection — nothing else — while the audit trail of what was believed and when survives. This is a property test, not a hope.

---

## 6. The two hard problems it actually solves

### (a) Entity resolution — a deterministic, auditable, consequence-ranked fold

I take PGS's three-band merge and consequence-ranked queue wholesale — every panel called this the single best honesty mechanism in the set — and Fold's `Split`-as-a-fact for correction.

```
core/resolve.py (pure):
  1. surface extraction (deterministic, at gather): adapters emit TYPED handles in signals
     (jira: assignee=john.doe@; pr: author=git:jdoe, files=src/auth/**; meeting: speaker="John")
  2. blocking keys (sub-quadratic): email local-part, git handle, glob overlap, jira label
  3. scoring (pure, weighted): exact keys → 1.0; email↔git via human-seeded map; glob↔label↔name co-occurrence
  4. THREE-BAND MERGE — not a boolean:
       >= τ_high (0.9)  → auto-merge (ALIAS_OF edge, by=resolver)
       τ_low..τ_high    → SUSPENDED. do NOT merge. → review queue. entities stay SEPARATE,
                          so unproven identity NEVER manufactures corroboration.
       < τ_low          → no edge.
```

**The honesty, concretely:**
- **Under-merge is the safe failure; over-merge is the dangerous one** (it manufactures false corroboration). Thresholds favor under-merging.
- **The consequence-ranked queue** (PGS, the standout): `maintain.lint` ranks to the *top* of the human queue exactly the below-threshold pairs that have **co-cited claims** — i.e. pairs where a merge *would create a validation*. **Human attention is proportional to stakes, not alphabet or volume.** Fold's complement: flag merges that *flipped a lifecycle outcome*.
- **`Split` is a first-class fact:** a wrong merge is undone by appending `Split`, and the next fold *re-evaluates every claim that depended on the bad merge* — demoting any whose only corroboration came from the phantom. Because it's a fold, the correction propagates completely; nothing stays silently wrong.

**Honest about residual drift:** I cannot guarantee zero spurious corroboration from entity error. I guarantee (a) every merge is a logged, replayable, gradeable edge with a reason; (b) merges that *create or flip* a validation are surfaced to a stakes-ranked queue; (c) any error is correctable by one appended `Split` with full propagation. Drift becomes a reviewable queue, not silent rot.

### (b) Fuzzy-source containment + causally-linked corroboration + the coverage blind spot

**Meetings (HUMAN trust)** distill to `Claimed` facts that are *structurally barred from pages* (§5). Three exits only: a crisp source corroborates independently → promote; a human appends `Confirmed(promote)`; or it stays quarantined in a queryable holding area — captured, cited, never injected.

**Causally-linked corroboration** — the trap where a meeting that *drove* a Jira ticket that a PR *closes* is three echoes of one origin, not three witnesses. The defense is PGS's `DERIVES`-edge-you-subtract, which all three panels named the cleanest formulation:
```python
# core/independence.py
def independent_sources(cites, sources, lineage) -> list[Witness]:
    # group cites by source_id, then COLLAPSE any group connected by a DERIVES chain
    # into ONE witness. corroboration counts only when it crosses causal clusters.
```
And the safety asymmetry that makes inference admissible: **an inferred `DERIVES` edge can only ever DEMOTE (collapse two witnesses to one), never validate.** A false causal hypothesis fails *safe* — it suppresses a true claim (visible, human reviews) but can never manufacture a false validation. `DERIVES` edges come from (i) **asserted lineage** — a source's own "created from meeting" link, `by=source_asserted`, high-precision; (ii) **inferred lineage** — same resolved entity + same predicate + tight time window + text overlap, `by=lineage:v2`, conservative, demote-only.

**The coverage blind spot** — a wiki 100% precise and 5% complete looks healthy. I **refuse to print a completeness %** (every panel: a fake green checkmark is worse than none). Instead, honest fold-derived *leading indicators*, framed as **work queues, not scores**:
- **orphan-evidence ratio** — `RawItem`s ingested but citing no claim → the distiller is under-reading a source.
- **PROPOSED-stall** — claims stuck with one HARD witness > N days → "almost validated, a second source probably exists but wasn't ingested."
- **source-diversity flag** — an area cited only by `jira` → likely incomplete, never marked healthy.
- **unresolved-entity rate** — `?`-prefixed entities per area → corroboration silently failing here.

The dashboard caption is blunt: *"precision is gated by cite-or-omit; recall is NOT measured — these are holes we can see; there may be holes we can't."*

**The residual drift I am honest about** (the gap *all three* designs share, per the panels, and I will not pretend to close):
1. **Undeclared causal links** — two HARD sources triggered by one root commit with no machine-readable link defeat `independent()`. I catch a fraction with a `same-root-commit` rule in the PR/agent gatherers; the rest is surfaced as a low-diversity / co-temporal-linked-actors flag. Relocated to a queue, not solved.
2. **The ingest-boundary blind spot** — every proxy measures *within the ingested set*; the meeting nobody recorded, the decision made in a DM, the source nobody wrote a gatherer for is *unknowable*. The proxies can read healthy while ingesting 5% of reality. Named, not closed.
3. **Triple-normalization is entity resolution on the predicate** — "was reverted" / "got rolled back" / "we backed out" must normalize to one `claim_id` or corroboration silently under-merges. **This is the crux all three designs waved past, and I make it explicit:** `core/normalize.py` gets the *same band/threshold/human-queue treatment* as person/component resolution — a controlled-ish predicate vocabulary with synonym classes, conservative (under-normalize is safe: a missed merge stays proposed, never falsely validates), and near-threshold normalization candidates land in the same consequence-ranked queue. I do **not** claim it's solved; I claim it's no longer hidden.

---

## 7. Forge relationship — decided from first principles

**Decision: Compile is a standalone program; forge is one *source*, not a dependency — and the seam is hardened against the exact thing the panels caught.**

Forge's razor is "if a feature can be a separate program reading the log, it is not in the core." Compile *is* that separate program — twice over (it reads forge's log; and within Compile the derivation is a separate pure program reading Compile's log). The prior wiki broke because the skill reached into agent internals (`messages/provider/models`) and snapped on a package rename. The structural fix:

- `adapters/sources/forge_session.py` is the **only** file where the token `forge` appears (import-linter-enforced). It reads forge's **published surface only** — never its kernel/provider/models. A package rename inside forge cannot reach a different *process* parsing *serialized bytes*.
- **Two logs, cleanly nested, and this is correct, not a smell:** forge owns "what the agent did"; Compile owns "what is known." Forge events enter as `Observed(RawItem{source_id:"forge"})`. No competing log over the same domain.
- **Coupling is one-directional and through files:** forge's agent *reads* `out/wiki/*.md` (cited context in its fixed slot); it never calls Compile's code. A broken wiki can never break the agent — the failure mode that motivated the redesign.

**The critical caveat ALL THREE panels raised, which I bake in:** I inspected the real surface — `.forge/sessions/3db752f6.json` is **per-turn summary JSON** (`{"id":...}` for `active.json`; a tiny turns blob with `assistantSummary/userInput/costUsd`), **not** the `fsync`'d append-only event-log serde all three designs assumed. Re-asserting an unverified "stable boundary" is *precisely* the mistake that broke the prior skill. So:

1. The forge adapter is written against **what actually exists** — lossy per-turn summaries — and **schema-validates that surface** (`spec/`), failing loudly in CI when it drifts.
2. Trust is assigned accordingly: a summary line is **SOFT at best, HUMAN where it's a paraphrase** — never HARD, because a summary is not a mechanically-enforced fact. This means forge-session claims behave like meeting lines: barred from pages until a crisp source (the repo, a PR, CI) corroborates. **The lossy surface is contained by the trust gate, automatically.**
3. If/when forge later publishes a true append-only event serde, that's a new adapter against a *better* surface — a versioned upgrade, not a rewrite.

---

## 8. Testing & CI-gated invariants

The architecture exists to make this section short and total: the load-bearing logic is pure, so it's exhaustively testable with no IO and no model.

**Property tests (core, no I/O, no LLM):**
- `fold(log) == fold(prefix) ⊕ fold(tail)` — incremental == cold. The replay guarantee.
- `fold(log) == fold(log)` under shuffled-then-total-ordered append — determinism.
- ∀ claims in `fold(any generated log)`: `cite_edges >= 1` — **cite-or-omit.**
- ∀ claims on a page: `max_trust >= SOFT or has_human_confirmation` — **trust gate.**
- lifecycle monotone: no fold sequence moves `validated → proposed` except by edge removal (Split/Retract/Contradict).
- `independent()` rejects same-causal-cluster cites; an inferred `DERIVES` can **only demote, never validate** (the safety asymmetry, as a test).
- `Retracted(f)` then re-fold removes **exactly** `f`'s transitive descendants and nothing else.
- `Split` un-merges and re-demotes claims that depended on the bad merge.

**Table tests:** `compile(graph, fact)` has a golden `(graph_in, fact, graph_out)` table — the whole engine in one readable table. Golden-derive fixtures (the three walkthroughs) run under `renderer_stub`, asserting graph state, never prose.

**CI commit-gates (the panels' core transplant — invariants that cannot be merged around):**
- `spec/validate` on the log — **cite-or-omit as `minItems:1` fails the commit.**
- import-linter contracts — purity, layering, `forge`-token-in-one-file.
- syscall/manifest check — a `pure=true` stage that opens a socket fails the build.
- **determinism gate** — `compile rebuild` twice, byte-compare the snapshot (minus LLM cache); any diff fails. ("Fully regenerable" as a test.)
- **no-hand-edit gate** — `out/wiki/` differs from `compile project` output → fail. (The wiki is a pure projection.)

**One integration suite (gated, rare):** real subprocess renderer against a few golden claim sets, asserting only that the render guard rejects invented citations and citations stay closed-world. Wording quality is a *separate, non-blocking* job — fidelity always gates, wording never does.

---

## 9. Build order

Sequenced **Small-Tools-first to de-risk buildability**, so the PGS graph machinery is *earned incrementally* rather than erected as a framework on day one. This is the explicit recommendation of all three panels.

**Day 1 value (deterministic, offline, no graph yet):**
1. **Freeze `spec/`** — `RawItem`, `Fact`, `Entry` JSON Schemas with cite-or-omit as `minItems:1`. Freeze it. This is the contract everything else tests against.
2. **`adapters/sources/{jira,pr}.py` + `core/fold` + `core/normalize` + `core/lifecycle` + `core/trust`** — gather → derive → promote over `facts.jsonl`, with the schema commit-gate live and the regenerability gate green. *Value on day one:* a cited, trust-gated, regenerable claim log from existing PR/Jira history, table-tested, zero LLM.
3. **`renderer_stub` + `present`** — flat templated cited pages. The whole pipeline runs offline in CI.

**Week 1:**
4. **Real subprocess renderer behind the two-sided guard.** Pages get prose; fidelity-gated.
5. **`maintain.lint`** — coverage proxies, citation-rot, contradiction sweep → `findings.jsonl`.
6. **`adapters/sources/{meeting,forge_session}.py`** — the fuzzy sources, contained by the trust gate from the start.

**Week 2+ — earn the graph (hardest, last):**
7. **Materialize `snapshot/graph.db` as a derived index**, with the rebuild-byte-compare gate.
8. **`core/resolve`** — blocking + scoring + three-band merge + the consequence-ranked queue.
9. **`core/lineage` + `core/independence`** — `DERIVES` inference (demote-only) and causal-cluster collapse.
10. **`policy/identity.fact.jsonl` + `Split`/`Confirmed` review flow** — the human-in-the-loop surface.

Entity resolution and causal-independence are **last because they are hardest and most drift-prone** — but the architecture lets them slot into the *existing* fold as new edge-types without touching anything upstream. You walk before you run; the graph is the right way to *think* from line one and the right thing to *build* in week two.

---

## 10. What it deliberately does NOT build

- **No mutable knowledge-base store, no editable wiki.** Humans append `Confirmed/Split/Retracted` facts; they never edit the projection. Non-negotiable — it's what makes replay and audit total.
- **No ML / embeddings / vector store / trained resolver.** Resolution is deterministic blocking + scoring + a human-seeded map + union-find. Embeddings reintroduce nondeterminism and a *silent over-merge* surface — the dangerous failure direction. Off-the-shelf LLM, one stage, render only.
- **No truth oracle / fact-checker.** Compile proves *provenance and trust-class*, never *correctness*. Cite-or-omit is honest about being provenance.
- **No completeness/recall %.** Refused on principle; only honest leading indicators, framed as queues.
- **No auto-promotion of fuzzy sources, ever.** HUMAN cannot self-promote regardless of repetition. No "enough meetings mentioned it" backdoor.
- **No general graph DB / Cypher engine.** SQLite + typed edge tables + hand-written pure queries over a *closed, small* edge vocabulary. The graph is a derived index, never a framework.
- **No real-time / streaming / webhooks / daemon.** Batch `pull(handle, cursor)` with persisted cursor-facts. Compounding is a build, not a stream. A cron in `front/` is the most we add.
- **No undeclared-causality inference by model.** We trust declared `Ref` edges, infer conservatively (demote-only), and *flag* suspicious co-temporal corroboration for humans. We do not trade a surfaced risk for an invisible one.
- **No cross-tenant permissions / access control.** The log is the trust boundary; auth is the host's problem.

---

## What changes when legacy is removed (vs the earlier "generalize-in-place" answer)

The earlier answer had to *generalize the existing ratchet repo in place* — keep `raw/` as-is, evolve the live `score_and_dedupe.py` (with its float confidence and count-based corroboration boost), preserve the markdown-skill-first shape, and migrate the current `.ratchet/metrics.json`. Removing that constraint changes five things concretely:

1. **Mutable confidence floats die.** The live system carries a confidence float and a count-based corroboration boost; generalizing in place meant taming them. Clean-sheet, **trust is a lattice and state is recomputed by a pure fold** — no mutable score to tame, the anti-pattern is *unrepresentable*.
2. **Corroboration stops being a count and becomes a causal-cluster cardinality.** In-place, "cited twice" was a counter. Clean-sheet, it's `len(independent_sources)` after `DERIVES`-subtraction — the causal-corroboration trap is defended *in the data model*, not patched in the validator.
3. **Entity identity stops being a string and becomes a graph node.** In-place, a reviewer was a `reviewers.yaml` entry and a name string. Clean-sheet, `Entity` is a derived cluster with gradeable `ALIAS_OF` edges and a consequence-ranked correction queue — the deep problem gets first-class machinery instead of a lookup table.
4. **The substrate becomes one append-only fact log instead of a `raw/` dir + a separate metrics file + a wiki.** Regenerability stops being "re-run the pipeline" and becomes "`fold(log)`" — a *property*, byte-compared in CI, not a procedure.
5. **The forge seam is rebuilt against what actually exists.** In-place, you inherit whatever the prior skill assumed. Clean-sheet, the adapter is written against the *verified* per-turn summary surface, schema-validated, and the lossy surface is contained by the trust gate — the exact over-confidence that snapped the last skill is structurally precluded.

What *doesn't* change, because the operating model was already right: cite-or-omit, the one-way ratchet, the trust gate, the single LLM stage, and "the human stays in the loop where the source is fuzzy." The clean sheet doesn't reinvent the safety model — it removes every place the legacy implementation could *cheat* it.

---

**Reference paths for the implementing engineer** (all absolute, greenfield under the repo root):
- Kernel + laws: `/Users/nikhilsalunke/ratchet/compile/core/{compile,fold,lifecycle,trust,resolve,lineage,independence,normalize,select,queue}.py`
- LLM seam: `/Users/nikhilsalunke/ratchet/compile/ports/renderer.py`, `/Users/nikhilsalunke/ratchet/compile/adapters/{renderer_subprocess,renderer_stub}.py`
- Frozen contract + gates: `/Users/nikhilsalunke/ratchet/compile/spec/` (build and freeze FIRST)
- Substrate: `/Users/nikhilsalunke/ratchet/compile/store/log/facts.jsonl` (source of truth); `/Users/nikhilsalunke/ratchet/compile/store/snapshot/graph.db` (derived index); `/Users/nikhilsalunke/ratchet/compile/store/out/wiki/` (projection)
- Forge adapter (verified surface): `/Users/nikhilsalunke/ratchet/compile/adapters/sources/forge_session.py` — reads `.forge/sessions/*.json` per-turn summaries, schema-validated, SOFT/HUMAN trust only.