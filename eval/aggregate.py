#!/usr/bin/env python3
"""
aggregate.py — collapse N per-run JSONs into a markdown comparison table.

Reads <results-dir>/runs/*.json and emits:
  - <results-dir>/summary.json    (machine-readable aggregate)
  - <results-dir>/summary.md      (markdown table for humans)

Groups runs by (tool, thinking) and reports median, p25, p75, min, max, n
for every numeric metric. Adds a comparison table forge-vs-flow (or
forge-adaptive vs forge-off) using the median.

Usage:
    aggregate.py --results-dir eval/results/<timestamp>
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# ── Metrics we aggregate ────────────────────────────────────────────────────
NUMERIC_FIELDS_TOP = [
    "real_seconds",
    "user_seconds",
    "sys_seconds",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "cache_read_tokens",
    "cache_write_tokens",
    "turns",
    "ctx_pct",
]
NUMERIC_FIELDS_CODE = [
    "impl_loc",
    "test_loc",
    "total_loc",
    "tests_collected",
    "pytest_passed",
    "pytest_failed",
    "pytest_seconds",
    "coverage_percent",
    "coverage_covered_lines",
    "coverage_total_lines",
    "hidden_passed",
    "hidden_collected",
    "ruff_issue_count",
    "main_file_count",  # kotlin
    "compile_seconds",  # kotlin
    "hidden_seconds",   # kotlin
]
BOOL_FIELDS_CODE = ["pytest_all_pass", "hidden_all_pass", "compile_pass"]


# ── Helpers ─────────────────────────────────────────────────────────────────
def get(d: dict, *path: str, default: Any = None) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def numeric_summary(values: list[float]) -> dict | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "median": statistics.median(s),
        "p25": s[max(0, n // 4 - 0)] if n >= 4 else s[0],
        "p75": s[min(n - 1, (3 * n) // 4)] if n >= 4 else s[-1],
        "min": s[0],
        "max": s[-1],
        "mean": round(statistics.fmean(s), 4),
        "stdev": round(statistics.stdev(s), 4) if n >= 2 else 0.0,
    }


def bool_summary(values: list[bool]) -> dict | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "pass_rate": sum(1 for v in vals if v) / len(vals)}


def fmt(x: Any, suffix: str = "", digits: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:,.{digits}f}{suffix}"
    if isinstance(x, int):
        return f"{x:,}{suffix}"
    return str(x)


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True, type=Path)
    args = ap.parse_args()

    runs_dir = args.results_dir / "runs"
    files = sorted(runs_dir.glob("*.json"))
    if not files:
        print(f"no runs found in {runs_dir}", file=sys.stderr)
        return 1

    runs = [json.loads(p.read_text()) for p in files]

    # Group by (tool, thinking) and by task
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in runs:
        key = (r.get("task", "?"), r.get("tool", "?"), r.get("thinking") or "default")
        groups[key].append(r)

    summary: dict[str, Any] = {
        "results_dir": str(args.results_dir),
        "run_count": len(runs),
        "groups": {},
    }

    for (task, tool, thinking), items in sorted(groups.items()):
        gkey = f"{task}::{tool}::{thinking}"
        agg: dict[str, Any] = {"task": task, "tool": tool, "thinking": thinking, "n": len(items)}

        for f in NUMERIC_FIELDS_TOP:
            agg[f] = numeric_summary([r.get(f) for r in items])
        for f in NUMERIC_FIELDS_CODE:
            agg[f] = numeric_summary([get(r, "code", f) for r in items])
        for f in BOOL_FIELDS_CODE:
            agg[f] = bool_summary([get(r, "code", f) for r in items])

        # Cost-per-output-token + thinking-tokens-share are derived metrics.
        out_t = [r.get("output_tokens") for r in items]
        cost = [r.get("cost_usd") for r in items]
        cpot = [c / o for c, o in zip(cost, out_t) if c and o]
        agg["cost_per_kout_usd"] = numeric_summary([c * 1000 for c in cpot])

        summary["groups"][gkey] = agg

    # ── Comparison: forge vs flow at default thinking ──────────────────────
    def med(group: dict, field: str) -> float | None:
        s = group.get(field)
        return s.get("median") if isinstance(s, dict) else None

    comparisons: list[dict] = []
    by_task = defaultdict(dict)
    for gkey, agg in summary["groups"].items():
        by_task[agg["task"]][(agg["tool"], agg["thinking"])] = agg

    for task, by_tool in by_task.items():
        # Pair every (tool,thinking) against every other for this task
        keys = sorted(by_tool.keys())
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                A = by_tool[a]
                B = by_tool[b]
                row = {
                    "task": task,
                    "a": f"{a[0]}/{a[1]}",
                    "b": f"{b[0]}/{b[1]}",
                    "deltas": {},
                }
                for f in (
                    "real_seconds",
                    "cost_usd",
                    "output_tokens",
                    "input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "turns",
                ):
                    av = med(A, f)
                    bv = med(B, f)
                    if av is None or bv is None:
                        row["deltas"][f] = None
                        continue
                    delta_pct = ((bv - av) / av * 100) if av else None
                    row["deltas"][f] = {"a": av, "b": bv, "b_minus_a": bv - av, "pct": delta_pct}
                comparisons.append(row)
    summary["comparisons"] = comparisons

    # ── Write summary.json ─────────────────────────────────────────────────
    (args.results_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # ── Write summary.md ───────────────────────────────────────────────────
    md: list[str] = []
    md.append(f"# Eval results — {args.results_dir.name}\n")
    md.append(f"Total runs: **{len(runs)}**  ·  Groups: **{len(summary['groups'])}**\n")

    # Detect whether any group is a Kotlin task (has compile_pass field)
    kotlin_mode = any(
        isinstance(agg.get("compile_pass"), dict)
        for agg in summary["groups"].values()
    )

    # ── 1. Headline table ─────────────────────────────────────────────────
    md.append("## 1. Headline (median across n runs)\n")
    if kotlin_mode:
        md.append(
            "| condition | n | wall (s) | cost ($) | out tok | turns | cache R | cache W "
            "| files | impl LOC | test LOC | compile% | own tests | own pass | hidden pass | arch% |"
        )
        md.append(
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
    else:
        md.append(
            "| condition | n | wall (s) | cost ($) | out tok | in tok | turns "
            "| cache R | cache W | own tests | pytest pass | hidden pass "
            "| coverage | ruff | impl LOC | test LOC |"
        )
        md.append(
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )

    for gkey in sorted(summary["groups"]):
        agg = summary["groups"][gkey]
        cond = f"**{agg['tool']}** / {agg['thinking']}"

        def m(f: str, digits: int = 2) -> str:
            s = agg.get(f)
            return fmt(s.get("median"), digits=digits) if isinstance(s, dict) else "—"

        def passrate(field: str) -> str:
            r = agg.get(field)
            if not isinstance(r, dict):
                return "—"
            return f"{r['pass_rate']*100:.0f}%"

        if kotlin_mode:
            # Architecture pass rate: count groups whose median run had
            # arch.passed == True. Computed inline because architecture is
            # nested.
            arch_pass_rate = "—"
            arch_passes = []
            for r in groups[(agg["task"], agg["tool"], agg["thinking"])]:
                ap = get(r, "code", "architecture", "passed")
                if ap is not None:
                    arch_passes.append(bool(ap))
            if arch_passes:
                arch_pass_rate = f"{sum(arch_passes)/len(arch_passes)*100:.0f}%"
            md.append(
                f"| {cond} | {agg['n']} "
                f"| {m('real_seconds')} | {m('cost_usd', 4)} "
                f"| {m('output_tokens', 0)} | {m('turns', 0)} "
                f"| {m('cache_read_tokens', 0)} | {m('cache_write_tokens', 0)} "
                f"| {m('main_file_count', 0)} "
                f"| {m('impl_loc', 0)} | {m('test_loc', 0)} "
                f"| {passrate('compile_pass')} "
                f"| {m('tests_collected', 0)} | {passrate('pytest_all_pass')} "
                f"| {m('hidden_passed', 0)}/{m('hidden_collected', 0)} "
                f"| {arch_pass_rate} |"
            )
        else:
            md.append(
                f"| {cond} | {agg['n']} "
                f"| {m('real_seconds')} | {m('cost_usd', 4)} "
                f"| {m('output_tokens', 0)} | {m('input_tokens', 0)} | {m('turns', 0)} "
                f"| {m('cache_read_tokens', 0)} | {m('cache_write_tokens', 0)} "
                f"| {m('tests_collected', 0)} | {passrate('pytest_all_pass')} "
                f"| {m('hidden_passed', 0)}/{m('hidden_collected', 0)} "
                f"| {m('coverage_percent', 1)}% | {m('ruff_issue_count', 0)} "
                f"| {m('impl_loc', 0)} | {m('test_loc', 0)} |"
            )

    # ── 2. Variance (range across runs) ───────────────────────────────────
    md.append("\n## 2. Variance check (n × wall / cost / output)\n")
    md.append(
        "| condition | wall median | wall range | wall stdev | cost median | cost range "
        "| out tok median | out tok range |"
    )
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for gkey in sorted(summary["groups"]):
        agg = summary["groups"][gkey]
        cond = f"{agg['tool']} / {agg['thinking']}"
        wall = agg.get("real_seconds") or {}
        cost = agg.get("cost_usd") or {}
        out_t = agg.get("output_tokens") or {}
        if not isinstance(wall, dict):
            continue
        md.append(
            f"| {cond} "
            f"| {fmt(wall.get('median'))}s "
            f"| {fmt(wall.get('min'))}–{fmt(wall.get('max'))}s "
            f"| {fmt(wall.get('stdev'))} "
            f"| ${fmt(cost.get('median'), digits=4)} "
            f"| ${fmt(cost.get('min'), digits=4)}–${fmt(cost.get('max'), digits=4)} "
            f"| {fmt(out_t.get('median'), digits=0)} "
            f"| {fmt(out_t.get('min'), digits=0)}–{fmt(out_t.get('max'), digits=0)} |"
        )

    # ── 3. Pairwise deltas ────────────────────────────────────────────────
    md.append("\n## 3. Pairwise comparisons (median b − median a)\n")
    md.append("| a | b | wall Δ | cost Δ | out tok Δ | turns Δ |")
    md.append("|---|---|---:|---:|---:|---:|")
    for c in comparisons:

        def d(field: str, suffix: str = "") -> str:
            v = c["deltas"].get(field)
            if v is None or v.get("pct") is None:
                return "—"
            return f"{v['b_minus_a']:+,.2f}{suffix} ({v['pct']:+.1f}%)"

        md.append(
            f"| {c['a']} | {c['b']} "
            f"| {d('real_seconds', 's')} | {d('cost_usd', '$')} "
            f"| {d('output_tokens')} | {d('turns')} |"
        )

    # ── 4. Quality bottom-line ────────────────────────────────────────────
    md.append("\n## 4. Quality bottom-line\n")
    md.append(
        "| condition | hidden pass | coverage (median) | ruff issues | tests written |"
    )
    md.append("|---|---:|---:|---:|---:|")
    for gkey in sorted(summary["groups"]):
        agg = summary["groups"][gkey]
        cond = f"{agg['tool']} / {agg['thinking']}"
        hp = agg.get("hidden_all_pass") or {}
        cov = agg.get("coverage_percent") or {}
        ruff = agg.get("ruff_issue_count") or {}
        tests = agg.get("tests_collected") or {}
        md.append(
            f"| {cond} "
            f"| {(hp.get('pass_rate', 0) * 100):.0f}% "
            f"| {fmt(cov.get('median'), digits=1) if isinstance(cov, dict) else '—'}% "
            f"| {fmt(ruff.get('median'), digits=0) if isinstance(ruff, dict) else '—'} "
            f"| {fmt(tests.get('median'), digits=0) if isinstance(tests, dict) else '—'} |"
        )

    (args.results_dir / "summary.md").write_text("\n".join(md) + "\n")
    print(f"wrote {args.results_dir / 'summary.json'}")
    print(f"wrote {args.results_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
