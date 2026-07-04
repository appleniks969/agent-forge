---
name: agent-forge-eval
description: >
  Run structured benchmarks comparing AI coding-agent quality and efficiency
  across simple and complex coding tasks. Use when evaluating forge after a
  refactor, when capturing a regression baseline, or when comparing forge
  against another CLI (e.g. claude CLI). Produces per-task metrics (turns, cost,
  tokens, cache hit rate, compile/test pass) and a markdown comparison table.
compatibility: >
  Requires forge (forge run -p --json) and optionally claude CLI. Also
  requires tsc (TypeScript compiler) and npx ts-node for quality checks on
  TypeScript complex tasks; kotlinc for C4; swiftc for C5. Run from any empty
  directory — the skill creates all files.
license: MIT
metadata:
  author: agent-forge
  version: "2.0"
  baseline-date: "2026-07-03"
  model: claude-sonnet-4-6
allowed-tools: Bash Read Write
---

# forge eval

Benchmark suite for forge (and comparable CLIs).
Eight tasks (3 simple TypeScript, 3 complex TypeScript, 1 Kotlin, 1 Swift),
graded on cost, turns, cache efficiency, compile cleanliness, and test
passage rate.

See [task definitions](references/tasks.md) and [known baselines](references/baselines.md).

---

## Before you start

**Isolation matters.** Some agent CLIs (notably `agent-flow`) explore parent
directories at start-up; if two tools run under a shared grandparent dir,
each can `ls ..` and pull the other's in-flight files into context. We have
seen this contaminate a comparison run, with one tool effectively
code-reviewing the other's output.

Rules:
1. **Disjoint parent dirs per tool** — never `/tmp/eval/{af,cc}/...`; use
   separate top-level parents instead.
2. **Sequential execution across tools** — never run two different tools on
   the same task in parallel. Same-tool variance runs in sibling dirs may run
   in parallel.
3. **Memory cleared between runs** — `/tmp/.agent-forge/` and any tool-specific
   memory locations. (forge session logs live in `~/.agent-forge/sessions/`,
   keyed by cwd; they do not leak between task dirs but can be pruned.)
4. **Credentials pinned per comparison.** forge's OAuth-token path
   (`CLAUDE_CODE_OAUTH_TOKEN`) and API-key path (`ANTHROPIC_API_KEY`) place
   and cache the system prompt differently, which shifts cost and cache
   metrics. All runs inside one baseline (and both sides of an A/B) MUST use
   the same credential type; record it in the baseline `source` field.
5. **Variance runs for complex tasks.** LLM run-to-run variance is large
   (observed 6–18 turns on the same task). Baselines record complex tasks
   as the aggregate of **3 runs each**; simple tasks may be single runs.
   Never conclude a regression or a win from one run.

```bash
rm -rf /tmp/.agent-forge/ /tmp/.agent-flow/ ~/.agent-flow/cache 2>/dev/null
rm -rf /tmp/forge-eval /tmp/claude-cli-eval
mkdir -p /tmp/forge-eval/{s1,s2,s3,c4,c5}
mkdir -p /tmp/forge-eval/{c1,c2,c3}_r{1,2,3}
mkdir -p /tmp/claude-cli-eval/{s1,s2,s3,c1,c2,c3,c4,c5}
```

---

## Running tasks

### forge (per task)

```bash
cd /tmp/forge-eval/<task> && forge run --json -p "<prompt>" > out.json 2> out.err
```

> `--json` writes one run-record JSON to stdout; rendering goes to stderr
> (`out.err` keeps the transcript for turn-by-turn forensics). The binary is
> installed via `uv tool install`; after editing forge source, reinstall with
> `uv tool install --force <repo-root>` or the benchmark measures stale code.

### claude CLI (per task, for comparison)

```bash
cd /tmp/claude-cli-eval/<task> && claude -p "<prompt>" --output-format json 2>&1 | tee out.txt
```

> **Notes:** (1) `claude -p --debug "<prompt>"` is wrong — `--debug`
> optionally consumes the next argument and will eat the prompt. Always put
> the prompt immediately after `-p`. (2) Pin the model explicitly
> (`--model claude-sonnet-4-6`) — the default may resolve to a different
> family and invalidate cost comparison.

---

## Prompts

Copy prompts from [references/tasks.md](references/tasks.md).

---

## Capturing metrics

### From forge `out.json`

```json
{"sid": "…", "outcome": "ok", "turns": 9,
 "usage": {"input_tokens": 18, "output_tokens": 3792,
            "cache_read_tokens": 31313, "cache_write_tokens": 16164},
 "cost": null, "error": null}
```

| Metric | JSON field |
|---|---|
| `turns` | `turns` |
| `inputTokens` | `usage.input_tokens` |
| `outputTokens` | `usage.output_tokens` |
| `cacheRead` | `usage.cache_read_tokens` |
| `cacheWrite` | `usage.cache_write_tokens` |
| `cost` | `cost` when non-null; otherwise compute from usage at $3/$15/$0.30/$3.75 per Mtok (in/out/cache-read/cache-write, sonnet-4-6 rates) |
| `cacheHitRate` | compute as `cacheRead / (cacheRead + inputTokens) * 100` |

### From claude CLI JSON output (`out.txt`, `--output-format json`)

| Metric | JSON field |
|---|---|
| `turns` | `num_turns` |
| `cost` | `total_cost_usd` |
| `outputTokens` | `usage.output_tokens` |
| `cacheRead` | `usage.cache_read_input_tokens` |
| `cacheWrite` | `usage.cache_creation_input_tokens` |

---

## Quality checks (complex tasks only)

After each complex task completes, from the task directory. **The compile
gate is bare-toolchain and strict on purpose** — validators that inject
implicit dependencies (ts-node bundles @types/node) mask exactly the defects
this gate exists to catch. The agent's self-check and this gate must be
byte-identical commands.

```bash
# C1–C3 (TypeScript): compile gate + tests
tsc --strict --noEmit *.ts
npx ts-node <testFile>

# C4 (Kotlin): compile gate + tests
kotlinc <files>.kt -include-runtime -d out.jar && java -jar out.jar

# C5 (Swift): compile gate + tests
swiftc <files>.swift -o tests && ./tests
```

Record:
- `compilesClean` — exit code 0 from the compile gate
- `testsPassed` — exit code 0 from the test run
- `assertions` — e.g. `14/14` from test output
- `qualityNotes` — manual notes on API design, type-system depth, edge cases

Adversarial probes (complex tier; run them, don't assume):
- **EventEmitter**: does `off(event, fn)` cancel a pending `once(event, fn)`?
  Do duplicate `on()` registrations both fire? Are listeners added during
  `emit` deferred to the next emit (snapshot dispatch)?
- **Rate limiter / LRU**: NaN/Infinity/zero/negative inputs; documented
  throw paths actually tested; import side effects (does importing the
  impl or test module execute anything?); named exports present.

> **Note:** claude CLI `--print` mode cannot write files unless invoked with
> `--allowedTools "Write,Bash,Read,Edit" --permission-mode acceptEdits`.
> Without those flags, complex task output is inline text only.

---

## Saving a new baseline

Once you have metrics for all tasks, save them as a JSON file:

```
eval/baseline/forge-<label>.json
```

Structure (mirror [assets/baseline-schema.json](assets/baseline-schema.json)):
complex-task entries carry per-run arrays plus the aggregate, e.g.
`"turns": [8, 11, 6], "avgTurns": 8.3`. Record the credential type and the
forge version/commit in `source`.

```json
{
  "label": "forge after <change> — <notes>",
  "capturedAt": "YYYY-MM-DD",
  "source": "automated — forge run -p --json @ <commit>, OAuth token, memory cleared, isolated parent dirs, 3 runs per complex task",
  "model": "claude-sonnet-4-6",
  "tool": "forge",
  "simpleTasks": { "…": "single-run entries as before" },
  "complexTasks": { "…": "3-run aggregate entries" },
  "platformTasks": {
    "C4_kotlinRateLimiter": { "…": "same shape as complex entries" },
    "C5_swiftLruCache": { "…": "same shape as complex entries" }
  },
  "overallQuality": { "score": 0.0, "basis": "", "strengths": [], "weaknesses": [] }
}
```

---

## Key metrics to watch

| Metric | Target | Red flag |
|---|---|---|
| Complex task total cost (C1–C3, avg of runs) | ~$0.29 | > $0.50 |
| Avg turns per complex task | ~7.7 | > 12 |
| Compile clean (bare, strict) | true | false |
| Tests pass | true | false |
| Quality score | 9.5+ | < 9.0 |

See [references/baselines.md](references/baselines.md) for the full history.
