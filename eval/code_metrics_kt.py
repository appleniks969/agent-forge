#!/usr/bin/env python3
"""
code_metrics_kt.py — code quality measurement for Kotlin/Gradle workdirs.

Mirrors code_metrics.py's contract for the Compose multi-file task:
  - count Kotlin LOC (impl + test)
  - run `gradle compileKotlin compileTestKotlin` → records compile pass
  - run `gradle test` → parses passed/failed/skipped
  - if hidden tests are provided, copy them into src/test/kotlin/_hidden/
    and re-run gradle test, then parse results separately
  - run architecture_check.py for layering rules
  - file/package counts

Emits JSON on stdout.

Note: requires JAVA_HOME pointing at JDK 17 (set by the harness).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_BLANK_OR_COMMENT_KT = re.compile(r"^\s*(//.*)?$")


def count_loc(path: Path) -> int:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return 0
    # Strip /* ... */ blocks first
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return sum(1 for line in text.splitlines() if not _BLANK_OR_COMMENT_KT.match(line))


def run_gradle(workdir: Path, args: list[str], timeout: int = 600, env: dict | None = None) -> tuple[int, str, float]:
    """Return (returncode, combined_output, elapsed_seconds)."""
    cmd = ["gradle", "--no-daemon", *args]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env or os.environ.copy(),
        )
        return proc.returncode, (proc.stdout + proc.stderr), round(time.monotonic() - t0, 2)
    except subprocess.TimeoutExpired as e:
        return -1, f"TIMEOUT after {timeout}s: {e}", round(time.monotonic() - t0, 2)
    except FileNotFoundError as e:
        return -1, f"gradle not found: {e}", 0.0


# ── Gradle test report parsing ──────────────────────────────────────────────
def parse_test_report(workdir: Path) -> dict:
    """Parse build/test-results/test/*.xml (JUnit XML) into pass/fail counts."""
    report_dir = workdir / "build" / "test-results" / "test"
    if not report_dir.exists():
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0, "no_report": True}

    total = passed = failed = skipped = 0
    failing_tests: list[str] = []
    for xml in report_dir.glob("*.xml"):
        text = xml.read_text(errors="replace")
        # <testsuite tests="N" failures="F" errors="E" skipped="S" ...>
        m = re.search(
            r'<testsuite[^>]*\btests="(\d+)"[^>]*\bfailures="(\d+)"[^>]*\berrors="(\d+)"[^>]*\bskipped="(\d+)"',
            text,
        )
        if not m:
            # try a different ordering
            t = re.search(r'tests="(\d+)"', text)
            f = re.search(r'failures="(\d+)"', text)
            e = re.search(r'errors="(\d+)"', text)
            s = re.search(r'skipped="(\d+)"', text)
            if t:
                ts, fs, es, ss = int(t.group(1)), int(f.group(1) if f else 0), int(e.group(1) if e else 0), int(s.group(1) if s else 0)
            else:
                continue
        else:
            ts, fs, es, ss = (int(g) for g in m.groups())
        total += ts
        failed += fs + es
        skipped += ss
        # Capture failing test names
        for tc in re.finditer(
            r'<testcase[^>]*\bname="([^"]+)"[^>]*\bclassname="([^"]+)"[^>]*>(.*?)</testcase>',
            text,
            flags=re.DOTALL,
        ):
            body = tc.group(3)
            if "<failure" in body or "<error" in body:
                failing_tests.append(f"{tc.group(2)}.{tc.group(1)}")
    passed = total - failed - skipped
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "failing_tests": failing_tests[:20],
    }


def discover_kt_files(workdir: Path) -> dict:
    main_root = workdir / "src" / "main" / "kotlin"
    test_root = workdir / "src" / "test" / "kotlin"
    main_files = list(main_root.rglob("*.kt")) if main_root.exists() else []
    test_files = list(test_root.rglob("*.kt")) if test_root.exists() else []
    # Exclude hidden tests dir from "agent's tests"
    agent_test_files = [p for p in test_files if "_hidden" not in p.parts]
    return {
        "main_files": main_files,
        "agent_test_files": agent_test_files,
        "all_test_files": test_files,
    }


def install_hidden_tests(workdir: Path, hidden_dir: Path) -> int:
    """Copy hidden_tests/*.kt → src/test/kotlin/_hidden/. Returns file count."""
    if not hidden_dir.exists():
        return 0
    dest = workdir / "src" / "test" / "kotlin" / "_hidden"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    count = 0
    for f in hidden_dir.glob("*.kt"):
        shutil.copy2(f, dest / f.name)
        count += 1
    return count


def remove_hidden_tests(workdir: Path) -> None:
    dest = workdir / "src" / "test" / "kotlin" / "_hidden"
    if dest.exists():
        shutil.rmtree(dest)


def run_architecture_check(repo_root: Path, workdir: Path) -> dict:
    script = repo_root / "eval/tasks/jetpack_compose_notes/architecture_check.py"
    if not script.exists():
        return {"architecture_skipped": "script not found"}
    try:
        proc = subprocess.run(
            ["python3", str(script), "--workdir", str(workdir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {"architecture_error": proc.stderr}
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        return {"architecture_error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--hidden-tests-dir", default=None, type=Path)
    ap.add_argument("--java-home", default=None,
                    help="Path to JDK 17. If omitted, uses $JAVA_HOME.")
    ap.add_argument("--repo-root", default=None, type=Path,
                    help="Repo root (for finding architecture_check.py).")
    args = ap.parse_args()

    args.workdir = args.workdir.resolve()
    repo_root = (args.repo_root or Path(__file__).resolve().parent.parent).resolve()

    env = os.environ.copy()
    if args.java_home:
        env["JAVA_HOME"] = args.java_home

    # Ensure the workdir is reset to a clean state w.r.t. hidden tests
    remove_hidden_tests(args.workdir)

    # ── 1. File discovery & LOC ────────────────────────────────────────────
    files = discover_kt_files(args.workdir)
    main_loc = sum(count_loc(p) for p in files["main_files"])
    test_loc = sum(count_loc(p) for p in files["agent_test_files"])

    # ── 2. Compile pass (agent's own code only, no hidden tests yet) ──────
    rc_compile, out_compile, secs_compile = run_gradle(
        args.workdir, ["compileKotlin", "compileTestKotlin", "-q"], env=env
    )
    compile_pass = rc_compile == 0

    # ── 3. Run agent's own tests ───────────────────────────────────────────
    rc_test, out_test, secs_test = run_gradle(
        args.workdir, ["test", "-q"], env=env
    )
    own_test_report = parse_test_report(args.workdir)
    own_test_pass = (
        rc_test == 0 and own_test_report.get("failed", 0) == 0 and own_test_report.get("total", 0) > 0
    )

    # ── 4. Run hidden tests (if provided) ──────────────────────────────────
    hidden_report = {}
    if args.hidden_tests_dir:
        installed = install_hidden_tests(args.workdir, args.hidden_tests_dir)
        if installed:
            # Clear previous test report before re-running
            test_results = args.workdir / "build" / "test-results" / "test"
            if test_results.exists():
                shutil.rmtree(test_results)
            rc_h, out_h, secs_h = run_gradle(
                args.workdir, ["test", "-q", "--rerun-tasks"], env=env, timeout=600
            )
            full_report = parse_test_report(args.workdir)
            # Hidden = full - own. Detect compile failure of hidden tests:
            # if full_total ≤ own_total, hidden tests didn't run at all
            # (gradle could not compile them, almost always due to import
            # mismatch with agent's package layout).
            own_total = own_test_report.get("total", 0)
            full_total = full_report.get("total", 0)
            own_passed = own_test_report.get("passed", 0)
            full_passed = full_report.get("passed", 0)
            own_failed = own_test_report.get("failed", 0)
            full_failed = full_report.get("failed", 0)
            hidden_total = max(0, full_total - own_total)
            hidden_passed = max(0, full_passed - own_passed)
            hidden_failed = max(0, full_failed - own_failed)
            compile_failed = hidden_total == 0
            hidden_report = {
                "hidden_files_installed": installed,
                "hidden_total": hidden_total,
                "hidden_passed": hidden_passed,
                "hidden_failed": hidden_failed,
                "hidden_all_pass": (
                    rc_h == 0 and hidden_failed == 0 and hidden_total > 0
                ),
                "hidden_compile_failed": compile_failed,
                "hidden_seconds": secs_h,
                "hidden_failing_names": [
                    n for n in full_report.get("failing_tests", [])
                    if n not in own_test_report.get("failing_tests", [])
                ],
            }
            # If hidden tests didn't compile, surface a sample of stderr
            # so the failure mode is debuggable.
            if compile_failed:
                hidden_report["hidden_compile_tail"] = (
                    out_h.strip().splitlines()[-15:] if out_h else []
                )
            remove_hidden_tests(args.workdir)
        else:
            hidden_report = {"hidden_skipped": "no .kt files in hidden_tests dir"}

    # ── 5. Architecture check ──────────────────────────────────────────────
    arch = run_architecture_check(repo_root, args.workdir)

    record = {
        "language": "kotlin",
        "main_files": [str(p.relative_to(args.workdir)) for p in files["main_files"]],
        "agent_test_files": [str(p.relative_to(args.workdir)) for p in files["agent_test_files"]],
        "main_file_count": len(files["main_files"]),
        "test_file_count": len(files["agent_test_files"]),
        "impl_loc": main_loc,
        "test_loc": test_loc,
        "total_loc": main_loc + test_loc,
        "compile_pass": compile_pass,
        "compile_seconds": secs_compile,
        "compile_tail": out_compile.strip().splitlines()[-5:] if not compile_pass else [],
        "tests_collected": own_test_report.get("total", 0),
        "pytest_passed": own_test_report.get("passed", 0),  # alias for aggregator parity
        "pytest_failed": own_test_report.get("failed", 0),
        "pytest_seconds": secs_test,
        "pytest_all_pass": own_test_pass,
        "test_report": own_test_report,
        "architecture": arch,
        **hidden_report,
        # aliases so aggregate.py picks them up
        "hidden_collected": hidden_report.get("hidden_total"),
        "coverage_percent": None,  # JaCoCo would go here; deferred
        "ruff_issue_count": None,  # ktlint would go here; deferred
    }
    json.dump(record, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
