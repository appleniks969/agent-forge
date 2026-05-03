# eval/ — agent-forge benchmarking harness

Track everything that helps explain *why* a coding agent is fast/slow/cheap/expensive.
Built specifically to handle high-variance LLM runs: every comparison is over
**N runs** with median + IQR, not single samples.

---

## Quick start

```bash
# Run a full matrix (3 conditions × N reps), aggregate, write summary.md
./eval/run_matrix.sh --task rate_limiter -n 5

# Or one run at a time
./eval/harness.sh --task rate_limiter --tool forge --thinking adaptive --run-index 1
./eval/harness.sh --task rate_limiter --tool flow  --run-index 1
./eval/harness.sh --task rate_limiter --tool forge --thinking off      --run-index 1

# Then aggregate whatever you have
./eval/aggregate.py --results-dir eval/results/<timestamp-or-name>
```

Results land in `eval/results/<timestamp>/`:
- `runs/<tool>_<thinking>_<NN>.json` — one record per run
- `raw/<tool>_<thinking>_<NN>.{stdout,stderr,time}` — archaeology
- `workdirs/<tool>_<thinking>_<NN>/` — the actual files the agent wrote
- `summary.json` — machine-readable aggregate
- `summary.md` — human-readable comparison table

---

## What gets tracked

Per run (single JSON record):

| Group | Field | Source |
|---|---|---|
| Identity | `task`, `tool`, `run_index`, `model`, `thinking`, `git_sha`, `captured_at` | harness args + `git rev-parse` |
| Timing | `real_seconds`, `user_seconds`, `sys_seconds`, `exit_code` | bash `time` builtin |
| Tokens | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | parsed from CLI footer |
| Cost | `cost_usd`, `ctx_pct` | parsed from CLI footer |
| Turns | `turns` | forge footer (flow does not surface in --print) |
| Code | `impl_loc`, `test_loc`, `total_loc`, `impl_files[]`, `test_files[]` | walks workdir, counts non-blank/non-comment Python LOC |
| Tests | `tests_collected`, `pytest_passed`, `pytest_failed`, `pytest_all_pass`, `pytest_seconds` | runs pytest in workdir |
| Raw | `stdout_bytes`, `stderr_bytes`, `workdir` | for archaeology |

Per group (aggregated across N runs):
- For every numeric field: `n`, `median`, `p25`, `p75`, `min`, `max`, `mean`, `stdev`
- For boolean fields (e.g. `pytest_all_pass`): `pass_rate`
- Derived: `cost_per_kout_usd` (cost per 1 K output tokens)

Per pair (every pair of groups for the same task):
- Median delta + percent change for `real_seconds`, `cost_usd`, `output_tokens`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `turns`

---

## Files

| File | What it does |
|---|---|
| `harness.sh` | One run end-to-end: spin fresh workdir, clear memory, run CLI under `time`, capture stdout/stderr, parse, run pytest, write run JSON |
| `run_matrix.sh` | Loop harness.sh across `(N reps × tools × thinking modes)`, then call aggregate |
| `parse_run.py` | Stdout/stderr → run JSON. Handles both forge footer `[N turn(s) · X in / Y out · $Z · ↓R ↑W · ctx: K%]` and flow line `[api] in=X out=Y cost=$Z` |
| `code_metrics.py` | Workdir → code JSON. Classifies impl/test files, counts LOC, runs pytest, captures pass/fail and timing |
| `system_prompt_size.py` | Offline: imports `agent_forge.prompts.build_chat_prompt`, prints per-section char/token counts. Use to test "is the prompt heavier?" hypotheses without paying for an LLM call |
| `aggregate.py` | runs/*.json → summary.{json,md}. Median/IQR/pairwise comparisons |
| `tasks/<id>/TASK.md` | Task definitions. Currently: `rate_limiter` (thread-safe TokenBucket + pytest) |

---

## Isolation rules (from `agent-forge-eval` skill)

The harness handles these automatically, but for the record:

1. **Disjoint workdirs** — every run gets its own `eval/results/<ts>/workdirs/<tag>/`, fresh.
2. **Memory cleared** — `rm -rf /tmp/.agent-forge/ /tmp/.agent-flow/ ~/.agent-flow/cache` before each run.
3. **Sequential execution** — never run two CLIs against the same task in parallel (skill notes some CLIs walk parent dirs at startup and can read each other's output).

---

## Known caveats

- **`--print` mode on agent-flow does not surface cache numbers or turn count.** Cache R/W and turn fields are `null` for flow rows. Compare on cost + output tokens instead.
- **Stricter LOC count.** `code_metrics.py` excludes blank lines and comment-only lines. Numbers will be ~30 % lower than `wc -l`. Apples-to-apples across tools regardless.
- **n=1 is meaningless.** flow's wall time has stdev ~47 s on rate_limiter. forge's is ~1 s. Always run ≥ 3 reps; ≥ 5 if you care about p25/p75.
- **One task is not a benchmark.** The TokenBucket task is small (single file pair, 6–7 turns). Tasks that exercise multi-file refactors or context pressure may rank tools differently. Add more tasks under `tasks/`.
- **`agent-flow --thinking` accepts off/low/medium/high (no `adaptive`).** When sweeping thinking modes, condition strings are tool-specific.

---

## Adding a task

### Python task (single-language, simple)

```bash
mkdir -p eval/tasks/<id>
$EDITOR eval/tasks/<id>/TASK.md           # the prompt
$EDITOR eval/tasks/<id>/hidden_tests.py   # evaluator-owned tests (optional)
./eval/harness.sh --task <id> --tool forge --run-index 1
```

The task workdir gets `TASK.md` copied in, the agent runs in that dir,
and `code_metrics.py` discovers whatever `.py` files it produces.

### Kotlin / Gradle task (multi-file, like `jetpack_compose_notes`)

```bash
mkdir -p eval/tasks/<id>/{template,hidden_tests}
$EDITOR eval/tasks/<id>/TASK.md
# template/ must contain build.gradle.kts, settings.gradle.kts,
# gradle.properties, src/main/kotlin/Main.kt — pre-warm with a build
# so first-run downloads don't poison the timing.
gradle -p eval/tasks/<id>/template compileKotlin
$EDITOR eval/tasks/<id>/hidden_tests/Hidden*.kt
EVAL_JAVA_HOME=/path/to/jdk17 ./eval/harness.sh --task <id> --tool forge --run-index 1
```

The harness auto-detects task type by checking for `template/build.gradle.kts`.
Kotlin tasks use `code_metrics_kt.py` with compile pass / gradle test /
hidden test diffing / `architecture_check.py`.

**Per-task timeouts.** Kotlin tasks routinely take 10–15 minutes per
agent run because of gradle compile + iteration loops. The bash `time`
builtin doesn't time out, but if you're invoking `harness.sh` from a
parent process (CI, another script), give it ≥ 1200 s.

---

## Validating a hypothesis: workflow

1. **Run a baseline matrix** before the change: `./run_matrix.sh -n 5 --results-dir eval/results/baseline`
2. Make the change.
3. **Run a candidate matrix**: `./run_matrix.sh -n 5 --results-dir eval/results/candidate`
4. Diff the two `summary.json` files (or hand-merge tables).
5. Treat any delta smaller than the larger group's stdev as noise.

