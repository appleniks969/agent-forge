# Known Baselines

Model: claude-sonnet-4-6 (all runs)

---

## Baseline index

| File | Label | Simple cost | Complex cost | Quality |
|---|---|---:|---:|---:|
| `claude-cli.json` | claude CLI reference (permanent) | $0.079 | $0.378 | 8.8/10 |
| `agent-flow-pre-ddd.json` | After 5 fixes, before DDD refactor (**target**) | $0.078 | $0.289 | 9.5/10 |
| `agent-flow-post-ddd.json` | After DDD refactor — clean run | $0.163 | $0.793 | 9.67/10 |

Full JSON files live in `eval/baseline/` at the project root.

---

## Efficiency snapshot (post-DDD vs. pre-DDD target)

### Simple tasks

| Task | Tool | Turns | Cost |
|---|---|---:|---:|
| S1 palindrome | agent-forge (post-DDD) | 4 | $0.041 |
| S1 palindrome | claude CLI | 1 | $0.025 |
| S2 TS Queue | agent-forge (post-DDD) | 4 | $0.045 |
| S2 TS Queue | claude CLI | 1 | $0.027 |
| S3 debounce | agent-forge (post-DDD) | 2 | $0.037 |
| S3 debounce | claude CLI | 1 | $0.026 |

### Complex tasks

| Task | Tool | Turns | Cost | Tests | Compile |
|---|---|---:|---:|:---:|:---:|
| C1 LRU Cache | agent-forge (post-DDD) | 14 | $0.214 | 51/51 ✅ | ✅ |
| C1 LRU Cache | claude CLI | 3 | $0.182 | 12/12 ✅ | ✅ (extracted) |
| C2 Rate Limiter | agent-forge (post-DDD) | 12 | $0.205 | 14/14 ✅ | ✅ |
| C2 Rate Limiter | claude CLI | 4 | $0.082 | 6/6 ✅ | ✅ (extracted) |
| C3 EventEmitter | agent-forge (post-DDD) | 14 | $0.250 | 27/27 ✅ | ✅ |
| C3 EventEmitter | claude CLI | 3 | $0.115 | 6/6 ✅ | ✅ (extracted) |

---

## Key observations

1. **agent-forge costs ~73% more** across all tasks — entirely explained by extra
   turns (plan → write → test → fix → summarise). claude CLI answers in 1–4 turns
   because it is stateless print mode.

2. **Quality delta on simple tasks is material**: agent-forge consistently adds
   leading-edge options, cancel/flush, isalnum cleaning, internal test runs.

3. **Clock seam (C2)** is the sharpest quality difference: agent-forge's
   `TokenBucketRateLimiter` accepts an injectable `now` function making tests fully
   deterministic. claude CLI's version blocks for ~110 ms in test 5.

4. **File delivery**: claude CLI `--print` mode cannot write files — all complex
   task output is inline text only. agent-forge delivers runnable, on-disk artefacts
   verified by `tsc --noEmit` + `npx ts-node`.

5. **Cache efficiency**: agent-forge hits 100% cache on turns 3+ within each task,
   dramatically reducing per-turn cost as tasks grow longer.

---

## Red flags vs. pre-DDD target

| Metric | Pre-DDD target | Red flag |
|---|---|---|
| Complex task total cost | ~$0.29 | > $0.50 |
| Avg turns per complex task | ~7.7 | > 12 |
| Compile clean | true | false |
| Tests pass | true | false |
| Quality score | 9.5+ | < 9.0 |
