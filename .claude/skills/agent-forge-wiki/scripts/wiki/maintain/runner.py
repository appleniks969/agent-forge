"""
wiki/maintain.py — detect stale areas and re-run gather on them.

Stage 6 of the compounding loop. The gather cursor advances every run, so
on its own gather catches up. But when a *specific area* gets a burst of
activity (10 commits in a week to src/payments/), we want to re-gather *just
that area* immediately — not wait for the next scheduled full gather.

Heuristic: an area is "stale" if the number of commits whose paths fall in
that area since the last gather exceeds ``threshold`` (default 10). We
read git log directly (cheap, no LLM, no network) to count.

Public:
    detect_stale_areas(repo_root, *, threshold=10) -> list[(area, n_commits)]
    run_maintain(repo_root, *, threshold=10) -> MaintainResult

This module deliberately does NOT call gather.run_gather with --area
filtering (yet) because the existing gatherers don't accept an area
filter. Instead we call gather() with `since=last_gather_ts` and clear the
dirty marker for every area we touched. That's the simplest thing that
plausibly works.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..storage import (
    areas_for_paths, clear_dirty, load_contexts, mark_dirty, read_cursor,
)


@dataclass(frozen=True)
class MaintainResult:
    stale_before: tuple[tuple[str, int], ...]   # (area, commits since gather)
    areas_refreshed: int
    artifacts_added: int
    errors: tuple[str, ...] = ()


# ── Public: stale detection ───────────────────────────────────────────────────

def detect_stale_areas(
    repo_root: Path | str,
    *,
    threshold: int = 10,
) -> list[tuple[str, int]]:
    """Return [(area, n_commits_since_last_gather)] sorted by n desc.

    Areas come from contexts.yaml. If contexts.yaml is missing, returns []
    — without area definitions we can't tell which commits belong where.
    Threshold filters: only areas with ≥ threshold commits are returned.
    """
    root = Path(repo_root)
    areas, _ = load_contexts(root)
    if not areas:
        return []

    last_gather = _last_gather_ts(root)
    counts = _count_commits_per_area(root, areas, since=last_gather)
    out = [(a, n) for a, n in counts.items() if n >= threshold]
    out.sort(key=lambda kv: -kv[1])
    return out


# ── Public: full maintain run ────────────────────────────────────────────────

async def run_maintain(
    repo_root: Path | str,
    *,
    threshold: int = 10,
) -> MaintainResult:
    """If any areas are stale, re-run gather (since=last_gather_ts) and clear
    the dirty markers. Returns counts for the CLI to print.
    """
    root = Path(repo_root)
    stale = detect_stale_areas(root, threshold=threshold)
    if not stale:
        return MaintainResult(
            stale_before=(),
            areas_refreshed=0,
            artifacts_added=0,
        )

    # Mark before so even if gather crashes we can resume.
    mark_dirty(root, [a for a, _ in stale])

    from ..gather import run_gather
    last = _last_gather_ts(root)
    try:
        result = await run_gather(root, since=last)
    except Exception as e:
        return MaintainResult(
            stale_before=tuple(stale),
            areas_refreshed=0,
            artifacts_added=0,
            errors=(f"gather failed: {type(e).__name__}: {e}",),
        )

    # Clear dirty markers for each area we just touched.
    for area, _ in stale:
        clear_dirty(root, area)

    return MaintainResult(
        stale_before=tuple(stale),
        areas_refreshed=len(stale),
        artifacts_added=result.artifacts_added,
        errors=tuple(result.errors),
    )


# ── Internals ─────────────────────────────────────────────────────────────────

def _last_gather_ts(repo_root: Path) -> datetime | None:
    cur = read_cursor(repo_root)
    last = cur.get("last_run_ts") if isinstance(cur, dict) else None
    if not isinstance(last, str):
        return None
    try:
        dt = datetime.fromisoformat(last)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _count_commits_per_area(
    repo_root: Path,
    areas: dict[str, list[str]],
    *,
    since: datetime | None,
) -> dict[str, int]:
    """Run `git log --name-only` once, bucket each commit's files into areas.

    Returns {area_name: n_commits} (zero entries omitted).
    """
    args = ["git", "log", "--name-only", "--pretty=format:%H"]
    if since is not None:
        # git log understands ISO dates.
        args.extend(["--since", since.isoformat()])

    # This function is sync (called from detect_stale_areas which is sync).
    # We use stdlib subprocess directly — no event loop here, no abort
    # signal needed. The wiki package's _subprocess.run is async-only.
    import subprocess
    try:
        proc = subprocess.run(  # noqa: S603 — controlled args
            args, cwd=str(repo_root), capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}

    # Parse: each commit starts with a SHA line, then a blank line, then files.
    # `--name-only --pretty=format:%H` yields:
    #     <sha1>
    #     path/a
    #     path/b
    #     <blank>
    #     <sha2>
    #     ...
    counts: dict[str, int] = {a: 0 for a in areas}
    current_paths: list[str] = []

    def _flush() -> None:
        if not current_paths:
            return
        for a in areas_for_paths(current_paths, areas):
            counts[a] = counts.get(a, 0) + 1

    lines = proc.stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            _flush()
            current_paths = []
            i += 1
            continue
        # If the line is a 40-char hex SHA, it's a new commit.
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            _flush()
            current_paths = []
            i += 1
            continue
        current_paths.append(line)
        i += 1
    _flush()

    return {a: n for a, n in counts.items() if n > 0}
