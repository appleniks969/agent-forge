#!/usr/bin/env python3
"""
rescan.py — re-run code_metrics.py against existing workdirs and update
the matching runs/<tag>.json file in place.

Use after adding new quality dimensions (coverage, ruff, hidden tests) to
existing eval results — no need to re-pay for agent runs.

Usage:
    rescan.py --results-dir eval/results/<dir> [--task rate_limiter]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True, type=Path)
    ap.add_argument("--task", default="rate_limiter")
    ap.add_argument("--venv-pytest", default="/tmp/harness-eval/venv/bin/pytest")
    ap.add_argument("--ruff-bin", default="/tmp/harness-eval/venv/bin/ruff")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    task_dir = repo_root / f"eval/tasks/{args.task}"

    # Detect task type
    if (task_dir / "template" / "build.gradle.kts").exists():
        task_type = "kotlin"
        code_metrics = repo_root / "eval/code_metrics_kt.py"
        hidden_dir = task_dir / "hidden_tests"
    else:
        task_type = "python"
        code_metrics = repo_root / "eval/code_metrics.py"
        hidden_file = task_dir / "hidden_tests.py"

    runs_dir = args.results_dir / "runs"
    workdirs_dir = args.results_dir / "workdirs"
    if not runs_dir.exists() or not workdirs_dir.exists():
        print(f"missing runs/ or workdirs/ in {args.results_dir}", file=sys.stderr)
        return 1
    print(f"[rescan] task_type={task_type}", file=sys.stderr)

    updated = 0
    skipped = 0
    for run_json in sorted(runs_dir.glob("*.json")):
        tag = run_json.stem  # e.g. forge_adaptive_01
        wd = workdirs_dir / tag
        if not wd.exists():
            print(f"  skip {tag}: no workdir", file=sys.stderr)
            skipped += 1
            continue

        if task_type == "kotlin":
            cmd = [
                "python3", str(code_metrics),
                "--workdir", str(wd),
                "--repo-root", str(repo_root),
                "--java-home",
                "/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home",
            ]
            if hidden_dir.exists():
                cmd += ["--hidden-tests-dir", str(hidden_dir)]
        else:
            cmd = [
                "python3", str(code_metrics),
                "--workdir", str(wd),
                "--venv-pytest", args.venv_pytest,
                "--ruff-bin", args.ruff_bin,
            ]
            if hidden_file.exists():
                cmd += ["--hidden-tests", str(hidden_file)]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            print(f"  fail {tag}: {proc.stderr[:200]}", file=sys.stderr)
            skipped += 1
            continue
        try:
            new_code = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            print(f"  fail {tag}: bad json from code_metrics: {e}", file=sys.stderr)
            skipped += 1
            continue

        record = json.loads(run_json.read_text())
        record["code"] = new_code
        run_json.write_text(json.dumps(record, indent=2))
        updated += 1
        if task_type == "kotlin":
            cp = new_code.get("compile_pass")
            ot = new_code.get("tests_collected", "?")
            op = new_code.get("pytest_passed", "?")
            ht = new_code.get("hidden_collected", "?")
            hp = new_code.get("hidden_passed", "?")
            arch = new_code.get("architecture", {}).get("passed", "?")
            print(f"  ok   {tag}: compile={cp}  own={op}/{ot}  hidden={hp}/{ht}  arch={arch}")
        else:
            cov = new_code.get("coverage_percent", "?")
            hp = new_code.get("hidden_passed", "?")
            ht = new_code.get("hidden_collected", "?")
            rc = new_code.get("ruff_issue_count", "?")
            print(f"  ok   {tag}: cov={cov}%  hidden={hp}/{ht}  ruff={rc}")

    print(f"\nupdated {updated} runs, skipped {skipped}")
    return 0 if updated else 1


if __name__ == "__main__":
    sys.exit(main())
