"""
wiki/metrics.py — append-only signal logs for the wiki feedback loop.

Three ungameable signals (borrowed from ratchet's design):

  - **citation rate**     did the agent cite something from curated/?
                          → if low, gather/compile is incomplete
  - **override rate**     did the user contradict a cited claim?
                          → curated/ has wrong info; rerun ratchet
  - **staleness lag**     time between last commit and last gather, per area
                          → gather is overdue; trigger maintain

This module is write-only by design. No analysis pipeline, no dashboards
— just JSONL on disk that any downstream tool (or `agent-forge wiki status`)
can summarise.

Files (one per signal):

    .agent-forge/metrics/citations.jsonl
    .agent-forge/metrics/overrides.jsonl
    .agent-forge/metrics/staleness.json   (snapshot, not append-only)

Public:
    record_citation(repo_root, *, session_id, turn, source_id, snippet)
    record_override(repo_root, *, session_id, turn, citation_id, user_correction)
    snapshot_staleness(repo_root) -> dict[area, days_lag]
    summarise(repo_root, *, last_n_days=14) -> MetricsSummary
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..storage import (
    contexts_path, list_artifacts, load_contexts, metrics_dir, raw_cache_dir,
)


# ── Path helpers ──────────────────────────────────────────────────────────────

def _citations_path(repo_root: Path | str) -> Path:
    return metrics_dir(repo_root) / "citations.jsonl"


def _overrides_path(repo_root: Path | str) -> Path:
    return metrics_dir(repo_root) / "overrides.jsonl"


def _staleness_path(repo_root: Path | str) -> Path:
    return metrics_dir(repo_root) / "staleness.json"


def _ensure() -> None:
    pass  # mkdir is done lazily on each write


def _atomic_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ── Recorders (write-only, never raise) ──────────────────────────────────────

def record_citation(
    repo_root: Path | str,
    *,
    session_id: str,
    turn: int,
    source_id: str,
    snippet: str = "",
) -> None:
    """Log that a turn cited something from curated/ (or raw/notes/).

    ``source_id`` is whatever the agent referenced — a curated file path,
    an artifact id, an ADR number. Snippet is the first ~80 chars of the
    cited material; useful for downstream "was this citation helpful?" review.

    Best-effort: any I/O error is swallowed.
    """
    try:
        entry = {
            "ts": _now(),
            "session_id": session_id,
            "turn": int(turn),
            "source_id": source_id,
            "snippet": (snippet or "")[:200],
        }
        _atomic_append(_citations_path(repo_root), json.dumps(entry))
    except Exception:
        pass


def record_override(
    repo_root: Path | str,
    *,
    session_id: str,
    turn: int,
    citation_id: str,
    user_correction: str,
) -> None:
    """Log a user correction of an agent claim that came from curated/.

    Wired to a `/wrong <text>` slash command (REPL) — but anyone can call it.
    """
    try:
        entry = {
            "ts": _now(),
            "session_id": session_id,
            "turn": int(turn),
            "citation_id": citation_id,
            "correction": (user_correction or "")[:500],
        }
        _atomic_append(_overrides_path(repo_root), json.dumps(entry))
    except Exception:
        pass


# ── Staleness snapshot ────────────────────────────────────────────────────────

def snapshot_staleness(repo_root: Path | str) -> dict[str, int]:
    """Return per-area lag in days: max(commit_ts) - max(gather_ts).

    Areas come from contexts.yaml (when present) plus any area mentioned on
    a commit's signals. A negative lag (gather newer than any commit) is
    clamped to 0. Missing data → omitted from the result.
    """
    root = Path(repo_root)
    areas, _ = load_contexts(root)
    declared = set(areas)

    last_commit_ts: dict[str, datetime] = {}
    for c in list_artifacts(root, kind="commit"):
        for area in _commit_areas(c, declared):
            ts = _as_dt(c.ts)
            if ts is None:
                continue
            if area not in last_commit_ts or ts > last_commit_ts[area]:
                last_commit_ts[area] = ts

    last_gather = _last_gather_ts(root)
    out: dict[str, int] = {}
    for area, ts in last_commit_ts.items():
        if last_gather is None:
            out[area] = -1   # never gathered
            continue
        delta = (ts - last_gather).days
        out[area] = max(delta, 0)

    # Persist a snapshot so `wiki status` can read it without rescanning.
    try:
        _staleness_path(root).parent.mkdir(parents=True, exist_ok=True)
        _staleness_path(root).write_text(
            json.dumps({"snapshot_ts": _now(), "lag_days": out}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return out


# ── Summary (read-side) ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class MetricsSummary:
    citations_n: int = 0
    overrides_n: int = 0
    citation_sources: tuple[tuple[str, int], ...] = ()  # (source_id, count) top 10
    stale_areas: tuple[tuple[str, int], ...] = ()       # (area, days) sorted desc
    window_days: int = 14


def summarise(repo_root: Path | str, *, last_n_days: int = 14) -> MetricsSummary:
    """Roll up the metrics files. Cheap; reads JSONL line-by-line."""
    root = Path(repo_root)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=last_n_days)

    cit_count = 0
    by_source: dict[str, int] = {}
    for entry in _read_jsonl(_citations_path(root)):
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        cit_count += 1
        sid = entry.get("source_id") or "?"
        by_source[sid] = by_source.get(sid, 0) + 1

    ovr_count = 0
    for entry in _read_jsonl(_overrides_path(root)):
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        ovr_count += 1

    stale = snapshot_staleness(root)
    sorted_stale = tuple(sorted(stale.items(), key=lambda kv: -kv[1])[:10])
    sorted_sources = tuple(
        sorted(by_source.items(), key=lambda kv: -kv[1])[:10]
    )

    return MetricsSummary(
        citations_n=cit_count,
        overrides_n=ovr_count,
        citation_sources=sorted_sources,
        stale_areas=sorted_stale,
        window_days=last_n_days,
    )


# ── Internals ─────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path):
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _parse_ts(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _as_dt(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return _parse_ts(v) if isinstance(v, str) else None


def _commit_areas(art, declared: set[str]) -> set[str]:
    """Resolve a commit's areas from .area or signals.areas."""
    out: set[str] = set()
    if art.area and art.area in declared:
        out.add(art.area)
    sig = art.signals or {}
    if isinstance(sig.get("areas"), list):
        for a in sig["areas"]:
            if isinstance(a, str) and a in declared:
                out.add(a)
    return out


def _last_gather_ts(repo_root: Path) -> datetime | None:
    """Return the timestamp of the most recent gather run.

    Uses the cursor file (.agent-forge/raw/cache/.cursor) which is updated
    at the end of every gather.
    """
    from ..storage import read_cursor
    cur = read_cursor(repo_root)
    last = cur.get("last_run_ts") if isinstance(cur, dict) else None
    return _parse_ts(last) if isinstance(last, str) else None
