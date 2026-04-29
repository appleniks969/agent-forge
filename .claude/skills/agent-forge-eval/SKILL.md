---
name: agent-forge-eval
description: >
  Run structured benchmarks comparing AI coding-agent quality and efficiency
  across simple and complex coding tasks. Use when evaluating agent-forge after a
  refactor, when capturing a regression baseline, or when comparing agent-forge
  against another CLI (e.g. claude CLI). Produces per-task metrics (turns, cost,
  tokens, cache hit rate, compile/test pass) and a markdown comparison table.
compatibility: >
  Requires agent-forge (agent-forge -p --debug) and optionally claude CLI.
  Also requires tsc (TypeScript compiler) and npx ts-node for quality checks on
  complex tasks. Run from any empty directory — the skill creates all files.
license: MIT
metadata:
  author: agent-forge
  version: "1.0"
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

Clear all accumulated agent-forge memory to prevent contamination, then create
fresh isolated directories for each task:

```bash
rm -rf /tmp/.agent-forge/
rm -rf /tmp/eval && mkdir -p /tmp/eval/af/{s1,s2,s3,c1,c2,c3} /tmp/eval/cc/{s1,s2,s3,c1,c2,c3}
```

---

## Running tasks

### agent-forge (per task)

```bash
cd /tmp/eval/af/<task> && agent-forge -p --debug "<prompt>" 2>&1 | tee out.txt
```

### claude CLI (per task, for comparison)

```bash
cd /tmp/eval/cc/<task> && claude -p "<prompt>" --output-format json 2>&1 | tee out.txt
```

> **Note:** `claude -p --debug "<prompt>"` is wrong — `--debug` optionally
> consumes the next argument and will eat the prompt. Always put the prompt
> immediately after `-p`.

---

## Prompts

Copy prompts from [references/tasks.md](references/tasks.md).

---

## Capturing metrics

### From agent-forge `--debug` output (`out.txt`)

| Metric | How to extract |
|---|---|
| `turns` | count of `API REQUEST` blocks |
| `cost` | sum of all `Cost: $X` lines |
| `inputTokens` | sum of all `Input: X tokens` lines (uncached) |
| `outputTokens` | sum of all `Output: X tokens` lines |
| `cacheRead` | sum of all `Cache Read: X tokens` lines |
| `cacheWrite` | sum of all `Cache Write: X tokens` lines |
| `cacheHitRate` | last turn's `Cache hit rate: X%` |

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
  "source": "automated — agent-forge -p --debug, memory cleared before run",
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
