#!/usr/bin/env python3
"""
code_metrics.py — measure code produced by an agent run.

Walks a workdir, classifies files as impl/test/other by filename, counts:
  - non-blank, non-comment LOC (impl_loc, test_loc)
  - file count (impl_files, test_files)
  - test count via pytest --collect-only -q
  - test pass/fail via pytest -q
  - timing for the pytest run

Usage:
    code_metrics.py --workdir /tmp/run-forge-1 \\
                    --venv-pytest /tmp/harness-eval/venv/bin/pytest \\
                    [--task-only-glob "rate_limiter*.py,test_rate_limiter*.py"]

Emits JSON on stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# ── LOC counter ─────────────────────────────────────────────────────────────
_BLANK_OR_COMMENT_PY = re.compile(r"^\s*(#.*)?$")


def count_loc(path: Path) -> int:
    """Non-blank, non-comment-only Python LOC. Multi-line strings counted as code."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return 0
    return sum(
        1 for line in text.splitlines() if not _BLANK_OR_COMMENT_PY.match(line)
    )


# ── File classification ─────────────────────────────────────────────────────
def is_test_file(p: Path) -> bool:
    n = p.name
    return n.startswith("test_") or n.endswith("_test.py")


def is_impl_file(p: Path) -> bool:
    if not p.suffix == ".py":
        return False
    if is_test_file(p):
        return False
    if p.name in {"__init__.py", "conftest.py"}:
        return False
    return True


def discover_files(workdir: Path, ignore_patterns: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in workdir.rglob("*.py"):
        rel = p.relative_to(workdir)
        if any(part in ("__pycache__", ".pytest_cache", "venv", ".venv") for part in rel.parts):
            continue
        if any(rel.match(pat) for pat in ignore_patterns):
            continue
        out.append(p)
    return out


# ── pytest invocation ───────────────────────────────────────────────────────
def pytest_collect(pytest_bin: str, workdir: Path) -> tuple[int, str]:
    """Return (collected_count, raw_output). collected_count = -1 on failure."""
    try:
        proc = subprocess.run(
            [pytest_bin, "--collect-only", "-q"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, str(e)
    out = proc.stdout + proc.stderr
    # pytest -q --collect-only ends with: "<N> tests collected"
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    return int(m.group(1)) if m else -1, out


def pytest_run(pytest_bin: str, workdir: Path) -> dict:
    """Run pytest -q. Return summary metrics."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [pytest_bin, "-q", "--tb=line"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        elapsed = time.monotonic() - t0
        out = proc.stdout + proc.stderr
        passed = failed = errors = 0
        # "11 passed in 0.04s" / "1 failed, 2 passed in 0.05s"
        pm = re.search(r"(\d+)\s+passed", out)
        fm = re.search(r"(\d+)\s+failed", out)
        em = re.search(r"(\d+)\s+errors?", out)
        if pm:
            passed = int(pm.group(1))
        if fm:
            failed = int(fm.group(1))
        if em:
            errors = int(em.group(1))
        return {
            "pytest_exit_code": proc.returncode,
            "pytest_passed": passed,
            "pytest_failed": failed,
            "pytest_errors": errors,
            "pytest_seconds": round(elapsed, 3),
            "pytest_all_pass": proc.returncode == 0 and failed == 0 and errors == 0,
            "pytest_tail": out.strip().splitlines()[-3:] if out else [],
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {
            "pytest_exit_code": -1,
            "pytest_error": str(e),
            "pytest_all_pass": False,
        }


# ── Main ────────────────────────────────────────────────────────────────────
def run_hidden_tests(pytest_bin: str, workdir: Path, hidden_tests_src: Path) -> dict:
    """Copy hidden_tests.py → workdir/test_hidden.py, run pytest on just it.

    Returns counts from the hidden suite — distinct from agent's own tests.
    """
    if not hidden_tests_src.exists():
        return {"hidden_skipped": "no hidden_tests.py provided"}
    dest = workdir / "test_hidden.py"
    dest.write_text(hidden_tests_src.read_text())
    try:
        proc = subprocess.run(
            [pytest_bin, "test_hidden.py", "-q", "--tb=no", "-p", "no:warnings"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"hidden_error": str(e), "hidden_all_pass": False}
    out = proc.stdout + proc.stderr
    pm = re.search(r"(\d+)\s+passed", out)
    fm = re.search(r"(\d+)\s+failed", out)
    em = re.search(r"(\d+)\s+errors?", out)
    passed = int(pm.group(1)) if pm else 0
    failed = int(fm.group(1)) if fm else 0
    errors = int(em.group(1)) if em else 0
    total = passed + failed + errors
    return {
        "hidden_collected": total,
        "hidden_passed": passed,
        "hidden_failed": failed,
        "hidden_errors": errors,
        "hidden_all_pass": proc.returncode == 0 and failed == 0 and errors == 0,
        "hidden_pass_rate": (passed / total) if total else None,
    }


def run_coverage(pytest_bin: str, workdir: Path, impl_files: list[Path]) -> dict:
    """Run agent's tests under pytest-cov, parse coverage.json for line %.

    Coverage is measured against the IMPL files only (excluding test files
    and the hidden test file, so coverage reflects how thoroughly the agent's
    own tests exercise their own implementation).
    """
    if not impl_files:
        return {"coverage_error": "no impl files to cover"}
    cov_targets = ",".join(p.stem for p in impl_files)  # module names
    cov_json = workdir / ".coverage.json"
    cov_json.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            [
                pytest_bin,
                "-q",
                "--tb=no",
                "-p",
                "no:warnings",
                f"--cov={cov_targets}",
                "--cov-report=json:.coverage.json",
                "--cov-report=",
                # Limit to agent's own tests (exclude test_hidden.py if present)
                "--ignore=test_hidden.py",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"coverage_error": str(e)}
    if not cov_json.exists():
        return {"coverage_error": "coverage.json not produced", "stderr_tail": proc.stderr[-200:]}
    try:
        cov_data = json.loads(cov_json.read_text())
    except json.JSONDecodeError as e:
        return {"coverage_error": f"bad json: {e}"}
    totals = cov_data.get("totals", {})
    return {
        "coverage_percent": round(totals.get("percent_covered", 0.0), 2),
        "coverage_covered_lines": totals.get("covered_lines", 0),
        "coverage_total_lines": totals.get("num_statements", 0),
        "coverage_missing_lines": totals.get("missing_lines", 0),
    }


def run_ruff(workdir: Path, impl_files: list[Path], ruff_bin: str = "ruff") -> dict:
    """Run `ruff check` on impl files, count issues by category."""
    if not impl_files:
        return {"ruff_skipped": "no impl files"}
    try:
        proc = subprocess.run(
            [ruff_bin, "check", "--output-format=json", *[str(p) for p in impl_files]],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {"ruff_skipped": f"ruff not found at {ruff_bin}"}
    except subprocess.TimeoutExpired:
        return {"ruff_skipped": "timeout"}
    try:
        issues = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        return {"ruff_error": "bad json", "stdout_head": proc.stdout[:200]}
    by_code: dict[str, int] = {}
    for it in issues:
        code = it.get("code") or "unknown"
        by_code[code] = by_code.get(code, 0) + 1
    return {
        "ruff_issue_count": len(issues),
        "ruff_issues_by_code": by_code,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--venv-pytest", required=True)
    ap.add_argument("--ruff-bin", default="ruff",
                    help="Path to ruff executable (default: 'ruff' from PATH).")
    ap.add_argument("--hidden-tests", default=None, type=Path,
                    help="Path to evaluator-owned hidden_tests.py (optional).")
    ap.add_argument(
        "--ignore",
        default="TASK.md,test_hidden.py,**/__pycache__/**",
        help="Comma-separated globs (relative to workdir) to ignore.",
    )
    args = ap.parse_args()

    # Resolve to absolute so subprocess cwd= and arg paths line up.
    args.workdir = args.workdir.resolve()

    # Remove leftovers from prior rescans so agent test counts stay clean.
    leftover = args.workdir / "test_hidden.py"
    leftover.unlink(missing_ok=True)
    leftover_cov = args.workdir / ".coverage.json"
    leftover_cov.unlink(missing_ok=True)
    leftover_dot = args.workdir / ".coverage"
    leftover_dot.unlink(missing_ok=True)

    ignore = tuple(p.strip() for p in args.ignore.split(",") if p.strip())
    files = discover_files(args.workdir, ignore)
    impl_files = [p for p in files if is_impl_file(p)]
    test_files = [p for p in files if is_test_file(p) and p.name != "test_hidden.py"]

    impl_loc = sum(count_loc(p) for p in impl_files)
    test_loc = sum(count_loc(p) for p in test_files)

    collected, _ = pytest_collect(args.venv_pytest, args.workdir)
    run_result = pytest_run(args.venv_pytest, args.workdir)
    coverage = run_coverage(args.venv_pytest, args.workdir, impl_files)
    ruff = run_ruff(args.workdir, impl_files, ruff_bin=args.ruff_bin)
    hidden = (
        run_hidden_tests(args.venv_pytest, args.workdir, args.hidden_tests)
        if args.hidden_tests else {"hidden_skipped": "not requested"}
    )

    record = {
        "impl_files": [str(p.relative_to(args.workdir)) for p in impl_files],
        "test_files": [str(p.relative_to(args.workdir)) for p in test_files],
        "impl_file_count": len(impl_files),
        "test_file_count": len(test_files),
        "impl_loc": impl_loc,
        "test_loc": test_loc,
        "total_loc": impl_loc + test_loc,
        "tests_collected": collected,
        **run_result,
        **coverage,
        **ruff,
        **hidden,
    }
    json.dump(record, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
