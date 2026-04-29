---
name: agent-forge-eval
description: >
  Run structured benchmarks comparing AI coding-agent quality and efficiency
  across simple and complex coding tasks. Use when evaluating agent-forge after a
  refactor, when capturing a regression baseline, or when comparing agent-forge
  against another CLI (e.g. claude CLI). Produces per-task metrics (turns, cost,
  tokens, cache hit rate, compile/test pass) and a markdown comparison table.
compatibility: >
  Requires agent-forge (agent-forge --prompt --verbose) and optionally claude CLI.
  Also requires tsc (TypeScript compiler) and npx ts-node for quality checks on
  complex tasks. Run from any empty directory — the skill creates all files.
license: MIT
metadata:
  author: agent-forge
  version: "1.1"
  baseline-date: "2026-04-26"
  model: claude-sonnet-4-6
allowed-tools: Bash Read Write
---

# agent-forge eval

Benchmark suite for agent-forge (and comparable CLIs).  
Six tasks (3 simple, 3 complex), graded on cost, turns, cache efficiency,
compile cleanliness, and test passage rate.

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
2. **Sequential execution** — never run two tools on the same task in
   parallel.
3. **Memory cleared between runs** — `/tmp/.agent-forge/` and any tool-specific
   memory locations.

```bash
rm -rf /tmp/.agent-forge/ /tmp/.agent-flow/ ~/.agent-flow/cache 2>/dev/null
rm -rf /tmp/agent-forge-eval /tmp/claude-cli-eval
mkdir -p /tmp/agent-forge-eval/{s1,s2,s3,c1,c2,c3}
mkdir -p /tmp/claude-cli-eval/{s1,s2,s3,c1,c2,c3}
```

---

## Running tasks

### agent-forge (per task)

```bash
cd /tmp/agent-forge-eval/<task> && agent-forge --prompt "<prompt>" --verbose 2>&1 | tee out.txt
```

> The current CLI uses `--prompt` (not `-p`) and `--verbose` (there is no
> `--debug` flag — `--debug-stream` exists but only emits raw provider
> events to stderr and is not needed for baseline capture).

### claude CLI (per task, for comparison)

```bash
cd /tmp/claude-cli-eval/<task> && claude -p "<prompt>" --output-format json 2>&1 | tee out.txt
```

> **Note:** `claude -p --debug "<prompt>"` is wrong — `--debug` optionally
> consumes the next argument and will eat the prompt. Always put the prompt
> immediately after `-p`.

---

## Prompts

Copy prompts from [references/tasks.md](references/tasks.md).

---

## Capturing metrics

### From agent-forge `--verbose` output (`out.txt`)

The current renderer prints **one footer line per session** in the format:

```
[N turn(s)  ·  Xin / Yout  ·  $0.XXXX  ·  ↓Z read  ↑W write  ·  ctx: K%]
```

| Metric | How to extract |
|---|---|
| `turns` | parse `N` from `[N turn(s)` |
| `cost` | parse the `$X.XXXX` token (after the green ANSI prefix) |
| `inputTokens` | parse `X` from `Xin / Yout` |
| `outputTokens` | parse `Y` from `Xin / Yout` |
| `cacheRead` | parse `Z` from `↓Z read` |
| `cacheWrite` | parse `W` from `↑W write` |
| `cacheHitRate` | not exposed; compute as `cacheRead / (cacheRead + inputTokens) * 100` if needed |

> **Per-turn metrics are no longer printed** by `--verbose`. The earlier
> `API REQUEST (turn N)` blocks belong to a previous CLI version. If you
> need raw per-turn data, run with `--debug-stream` (writes stream events
> to stderr), but baseline capture only requires the session footer.

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

After each complex task completes, from the task directory:

```bash
# TypeScript compile check
tsc --noEmit

# Run tests (replace <testFile> with the actual filename)
npx ts-node <testFile>
```

Record:
- `compilesClean` — exit code 0 from `tsc --noEmit`
- `testsPassed` — exit code 0 from `npx ts-node`
- `assertions` — e.g. `14/14` from test output
- `qualityNotes` — manual notes on API design, TypeScript depth, edge cases

> **Note:** claude CLI `--print` mode cannot write files (permission prompts
> are suppressed). Complex task output is inline text only — quality must be
> assessed by reading the `result` field in the JSON.

---

## Saving a new baseline

Once you have metrics for all six tasks, save them as a JSON file:

```
eval/baseline/agent-forge-<label>.json
```

Structure (mirror [assets/baseline-schema.json](assets/baseline-schema.json)):

```json
{
  "label": "agent-forge after <change> — <notes>",
  "capturedAt": "YYYY-MM-DD",
  "source": "automated — agent-forge --prompt --verbose, memory cleared and isolated parent dirs per tool",
  "model": "claude-sonnet-4-6",
  "tool": "agent-forge",
  "simpleTasks": {
    "total": { "cost": 0.0, "tasks": 3, "avgTurns": 0 },
    "S1_palindrome": { "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0 },
    "S2_queue":      { "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0 },
    "S3_debounce":   { "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0 }
  },
  "complexTasks": {
    "total": { "cost": 0.0, "tasks": 3, "avgTurns": 0 },
    "C1_lruCache":    { "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0, "compilesClean": true, "testsPassed": true, "testCount": 0, "testPassCount": 0, "qualityScore": 0.0, "qualityNotes": "" },
    "C2_rateLimiter": { "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0, "compilesClean": true, "testsPassed": true, "testCount": 0, "testPassCount": 0, "qualityScore": 0.0, "qualityNotes": "" },
    "C3_eventEmitter":{ "turns": 0, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "cacheRead": 0, "cacheWrite": 0, "cacheHitRate": 0.0, "compilesClean": true, "testsPassed": true, "testCount": 0, "testPassCount": 0, "qualityScore": 0.0, "qualityNotes": "" }
  },
  "overallQuality": { "score": 0.0, "basis": "", "strengths": [], "weaknesses": [] }
}
```

---

## Key metrics to watch

| Metric | Pre-DDD target | Red flag |
|---|---|---|
| Complex task total cost | ~$0.29 | > $0.50 |
| Avg turns per complex task | ~7.7 | > 12 |
| Compile clean | true | false |
| Tests pass | true | false |
| Quality score | 9.5+ | < 9.0 |

See [references/baselines.md](references/baselines.md) for the full history.
