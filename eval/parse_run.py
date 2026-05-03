#!/usr/bin/env python3
"""
parse_run.py — Extract structured metrics from agent-forge / agent-flow stdout.

Reads one stdout file + one stderr file + one /usr/bin/time output, emits a
single JSON object on stdout describing the run.

Forge footer (--verbose):
    [N turn(s)  ·  Xin / Yout  ·  $0.XXXX  ·  ↓Z read  ↑W write  ·  ctx: K%]

Flow footer (--verbose --print):
    [api] in=X out=Y cost=$0.XXXX
    (cache numbers not surfaced in --print as of agent-flow current)

Time wrapper (zsh `time`, written to stderr by harness.sh):
    real <s>
    user <s>
    sys  <s>
    EXIT=<code>

Usage:
    parse_run.py --tool forge --stdout f.out --stderr f.err --time t.txt \\
                 --task rate_limiter --run-index 1 \\
                 --thinking adaptive --model claude-sonnet-4-6 \\
                 --workdir /tmp/run-forge-1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── ANSI stripping ──────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ── Forge footer parser ────────────────────────────────────────────────────
# Example (post-strip):
#   [7 turn(s)  ·  58in / 5,198out  ·  $0.1195  ·  ↓33,209 read  ↑8,363 write  ·  ctx: 3%]
_FORGE_FOOTER_RE = re.compile(
    r"\[(?P<turns>\d+)\s+turn\(s\).*?"
    r"(?P<input>[\d,]+)\s*in\s*/\s*(?P<output>[\d,]+)\s*out.*?"
    r"\$(?P<cost>[\d.]+).*?"
    r"↓(?P<cache_read>[\d,]+)\s*read.*?"
    r"↑(?P<cache_write>[\d,]+)\s*write.*?"
    r"ctx:\s*(?P<ctx_pct>\d+)%\]",
    re.DOTALL,
)


def parse_forge(stdout_text: str) -> dict:
    text = strip_ansi(stdout_text)
    m = _FORGE_FOOTER_RE.search(text)
    if not m:
        return {"_parse_error": "forge footer not found"}

    def to_int(s: str) -> int:
        return int(s.replace(",", ""))

    return {
        "turns": int(m.group("turns")),
        "input_tokens": to_int(m.group("input")),
        "output_tokens": to_int(m.group("output")),
        "cost_usd": float(m.group("cost")),
        "cache_read_tokens": to_int(m.group("cache_read")),
        "cache_write_tokens": to_int(m.group("cache_write")),
        "ctx_pct": int(m.group("ctx_pct")),
    }


# ── Flow footer parser ─────────────────────────────────────────────────────
# Stderr line:  [api] in=8 out=4053 cost=$0.0962
_FLOW_FOOTER_RE = re.compile(
    r"\[api\]\s+in=(?P<input>\d+)\s+out=(?P<output>\d+)\s+cost=\$(?P<cost>[\d.]+)"
)


def parse_flow(stdout_text: str, stderr_text: str) -> dict:
    """Flow prints the [api] line to stderr, and conversation to stdout."""
    combined = strip_ansi(stdout_text + "\n" + stderr_text)
    m = _FLOW_FOOTER_RE.search(combined)
    if not m:
        return {"_parse_error": "flow [api] footer not found"}
    return {
        "input_tokens": int(m.group("input")),
        "output_tokens": int(m.group("output")),
        "cost_usd": float(m.group("cost")),
        # Flow does not surface in --print mode:
        "turns": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "ctx_pct": None,
    }


# ── /usr/bin/time wrapper output parser ─────────────────────────────────────
_TIME_RE = re.compile(r"^(real|user|sys)\s+([\d.]+)", re.MULTILINE)
_EXIT_RE = re.compile(r"^EXIT=(\d+)", re.MULTILINE)


def parse_time(time_text: str) -> dict:
    out: dict = {"real_seconds": None, "user_seconds": None, "sys_seconds": None, "exit_code": None}
    for kind, val in _TIME_RE.findall(time_text):
        out[f"{kind}_seconds"] = float(val)
    em = _EXIT_RE.search(time_text)
    if em:
        out["exit_code"] = int(em.group(1))
    return out


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", required=True, choices=["forge", "flow"])
    ap.add_argument("--stdout", required=True, type=Path)
    ap.add_argument("--stderr", required=True, type=Path)
    ap.add_argument("--time", required=True, type=Path, dest="time_path")
    ap.add_argument("--task", required=True)
    ap.add_argument("--run-index", required=True, type=int)
    ap.add_argument("--thinking", default=None)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--git-sha", default="unknown")
    args = ap.parse_args()

    stdout_text = args.stdout.read_text(errors="replace") if args.stdout.exists() else ""
    stderr_text = args.stderr.read_text(errors="replace") if args.stderr.exists() else ""
    time_text = args.time_path.read_text(errors="replace") if args.time_path.exists() else ""

    if args.tool == "forge":
        agent_metrics = parse_forge(stdout_text)
    else:
        agent_metrics = parse_flow(stdout_text, stderr_text)

    time_metrics = parse_time(time_text)

    record = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": args.task,
        "tool": args.tool,
        "run_index": args.run_index,
        "model": args.model,
        "thinking": args.thinking,
        "workdir": str(args.workdir),
        "git_sha": args.git_sha,
        "stdout_bytes": len(stdout_text),
        "stderr_bytes": len(stderr_text),
        **time_metrics,
        **agent_metrics,
    }

    json.dump(record, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
