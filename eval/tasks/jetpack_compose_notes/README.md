# Jetpack Compose Notes — multi-file Kotlin eval task

## What it tests

A 10-file Compose Multiplatform Desktop notes app spanning **4 layers**
(`model` → `data` → `viewmodel` → `ui`). Discriminates agents on:

| Dimension | Metric |
|---|---|
| Architectural decomposition | `architecture_check.py` — ≥ 5 files, ≥ 3 layers, no forbidden imports |
| Layering discipline | ViewModel must not import `androidx.compose.ui.*` etc.; UI layer cannot leak into data/model |
| Compile correctness | `gradle compileKotlin compileTestKotlin` exit 0 |
| Self-test discipline | Agent's own JUnit 5 tests pass (`gradle test`) |
| Spec correctness | 12 evaluator-owned hidden tests against `NotesRepository` + `NotesViewModel` public APIs |
| Cost/speed | Same as Python tasks (wall, cost, output tokens, turns) |

## Why Compose **Multiplatform Desktop**, not Android

Pure JVM. No Android SDK, no emulator, no instrumentation. The Compose
APIs the agent must use (`@Composable`, `remember`, `MutableState`,
`Column`, `Text`, `collectAsState`) are the **same imports** as Android's
Compose. The renderer is the only thing that differs, and we don't render.

This means: any machine with JDK 17 + Gradle can run the eval.

## Why we mandate package names in TASK.md

Hidden tests must `import data.InMemoryNotesRepository`. If the agent uses
`com.foo.notes.data.InMemoryNotesRepository`, hidden tests can't compile.
Two options were considered:

1. **Mandate package names** (chosen) — TASK.md fixes `model`, `data`,
   `viewmodel`, `ui`, `ui.components`. Simple, deterministic.
2. Reflection-based hidden tests — would work but bloats hidden tests
   with `Class.forName` calls and obscures what's actually being tested.

If the agent ignores the package mandate, the eval correctly reports
`hidden_compile_failed: true` with the gradle stderr tail, and we'll see
it in the rescan output.

## Layout

```
jetpack_compose_notes/
├── README.md                          (this file)
├── TASK.md                            (the prompt agents receive)
├── architecture_check.py              (layering + file/package count rule)
├── template/                          (copied to each agent's workdir)
│   ├── build.gradle.kts               (Compose Desktop, Kotlin 1.9.23, JDK 17)
│   ├── settings.gradle.kts
│   ├── gradle.properties              (parallel + caching on)
│   └── src/main/kotlin/Main.kt        (placeholder, agent replaces)
└── hidden_tests/
    ├── HiddenRepositoryTest.kt        (7 tests — CRUD + concurrency)
    └── HiddenViewModelTest.kt         (5 tests — state transitions)
```

## Verification flow per agent run

The harness:

1. `rsync` the `template/` directory into a fresh workdir.
2. Drop in `TASK.md`.
3. Run the agent (forge or flow) against the workdir.
4. Run `code_metrics_kt.py`:
   - Discover `*.kt` files, count main/test LOC.
   - `gradle compileKotlin compileTestKotlin` → `compile_pass` boolean.
   - `gradle test` → parse `build/test-results/test/*.xml` → `tests_collected`, `pytest_passed`, `pytest_failed`.
   - Copy `hidden_tests/*.kt` → `src/test/kotlin/_hidden/`, `gradle test --rerun-tasks` → diff vs prior report → `hidden_passed`, `hidden_total`, `hidden_compile_failed`.
   - Run `architecture_check.py` → `architecture.passed`, `layers_seen`, violations list.

## Running it

Pre-warm Gradle cache (one-time, ~2 min download):

```bash
JAVA_HOME=/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home \
  gradle --no-daemon -p eval/tasks/jetpack_compose_notes/template compileKotlin
```

One run:

```bash
EVAL_JAVA_HOME=/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home \
  bash eval/harness.sh --task jetpack_compose_notes --tool forge --thinking adaptive --run-index 1
```

Full matrix (n=3 per condition, ~1.5 hours):

```bash
EVAL_JAVA_HOME=/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home \
  bash eval/run_matrix.sh --task jetpack_compose_notes -n 3
```

Re-run quality eval on existing workdirs (no agent re-runs):

```bash
python3 eval/rescan.py --task jetpack_compose_notes --results-dir eval/results/<dir>
python3 eval/aggregate.py --results-dir eval/results/<dir>
```

## Known limits

- **No UI behavior testing.** We compile Composables but don't render or
  click them. To get UI behavior we'd need `androidx.compose.ui.test.junit4`
  with a `runComposeUiTest {}` block — roughly doubles the eval surface
  but is feasible if needed later.
- **No JaCoCo coverage.** Could add via gradle plugin; not yet wired into
  `code_metrics_kt.py`.
- **No ktlint / detekt.** Same — easy to add, not wired.
- **Task takes longer than rate_limiter** — agents need 5–15 minutes,
  budget timeouts accordingly. Set `--timeout` in harness if running on
  CI.
