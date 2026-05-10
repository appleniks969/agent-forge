"""
gather/discovery.py — find user gatherers, topo-sort, run with isolation.

The single orchestrator entry point. Composes:

  * BUILTINS from gather/builtin/__init__.py
  * User .py files in .agent-forge/gatherers/ (each may define one or more
    Gatherer subclasses)
  * Topological sort by `runs_after` so hotspots runs after prs+git_history
  * Per-gatherer asyncio.wait_for timeout
  * Per-gatherer try/except — one failure logs to .gather.log and the rest
    of the run continues
  * Atomic artifact writes via storage.write_artifact()
  * SHA-cache dedup so repeat runs are cheap
  * Cursor persistence
  * Dirty-area marking (no-op for MVP 1; useful in MVP 4)
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..storage import (
    areas_for_paths, ensure_layout, gatherers_dir,
    load_contexts, log_error, mark_dirty,
    read_cursor, sha_record, sha_seen, write_artifact, write_cursor,
)
from ..types import Artifact, Gatherer, GatherResult
from .builtin import BUILTINS

# Default lookback when there's no cursor yet — one year is a reasonable
# default for most repos; the cursor takes over from there.
_DEFAULT_LOOKBACK = timedelta(days=365)


# ── User gatherer discovery ───────────────────────────────────────────────────

def _load_user_gatherers(repo_root: Path) -> list[Gatherer]:
    """Import every .py file in .agent-forge/gatherers/, return Gatherer instances.

    Each module may define multiple Gatherer subclasses; we instantiate all
    of them. Modules that fail to import are logged and skipped — never crash
    the whole gather.
    """
    d = gatherers_dir(repo_root)
    if not d.exists():
        return []
    out: list[Gatherer] = []
    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"_agent_forge_user_gatherers.{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception as e:
            log_error(repo_root, f"user/{path.name}", f"import failed: {e!r}")
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is Gatherer:
                continue
            if not issubclass(obj, Gatherer):
                continue
            if obj.__module__ != mod_name:
                continue   # don't double-instantiate re-imported builtins
            try:
                inst = obj()
            except Exception as e:
                log_error(repo_root, f"user/{path.name}/{obj.__name__}", f"instantiation failed: {e!r}")
                continue
            if not getattr(inst, "name", ""):
                # User gatherers must declare a name; default to class name.
                inst.name = obj.__name__.lower()  # type: ignore[misc]
            out.append(inst)
    return out


# ── Topological sort ──────────────────────────────────────────────────────────

def _topo_sort(gatherers: list[Gatherer]) -> list[Gatherer]:
    """Order gatherers by `runs_after` (Kahn's algorithm).

    Edges point dependency → dependent. Cycles or missing dependencies fall
    back to "no edge" so the gatherer still runs (defensively — a typo in
    runs_after shouldn't break the whole pipeline).
    """
    by_name: dict[str, Gatherer] = {g.name: g for g in gatherers}
    in_degree: dict[str, int] = {g.name: 0 for g in gatherers}
    edges: dict[str, list[str]] = defaultdict(list)
    for g in gatherers:
        for dep in g.runs_after:
            if dep in by_name:
                edges[dep].append(g.name)
                in_degree[g.name] += 1
    ready = [name for name, deg in in_degree.items() if deg == 0]
    ordered: list[str] = []
    while ready:
        n = ready.pop(0)
        ordered.append(n)
        for dependent in edges[n]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) < len(gatherers):
        # Cycle; append the rest in declaration order.
        for g in gatherers:
            if g.name not in ordered:
                ordered.append(g.name)
    return [by_name[n] for n in ordered]


# ── Sandboxing artifact writes ────────────────────────────────────────────────

def _persist(repo_root: Path, artifact: Artifact) -> bool:
    """Write artifact via storage; return True if newly written, False if dedup'd."""
    if sha_seen(repo_root, artifact.id):
        return False
    write_artifact(repo_root, artifact)
    sha_record(repo_root, artifact.id)
    return True


# ── Top-level orchestrator ────────────────────────────────────────────────────

async def run_gather(
    repo_root: Path | str,
    since: datetime | None = None,
    *,
    only: list[str] | None = None,
) -> GatherResult:
    """Run all gatherers (or a subset) and persist their artifacts.

    Parameters
    ----------
    repo_root  Project root containing .agent-forge/.
    since      Lower bound for first-time gathers. Defaults to 1 year ago.
    only       If given, run only gatherers whose name appears in this list.
    """
    root = Path(repo_root)
    ensure_layout(root)
    cursor = read_cursor(root)
    last_run = cursor.get("last_run_ts")
    if since is None:
        if last_run:
            try:
                since = datetime.fromisoformat(last_run)
            except ValueError:
                since = None
        if since is None:
            since = datetime.now(tz=timezone.utc) - _DEFAULT_LOOKBACK
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    gatherers: list[Gatherer] = [cls() for cls in BUILTINS]
    gatherers.extend(_load_user_gatherers(root))
    if only:
        wanted = set(only)
        gatherers = [g for g in gatherers if g.name in wanted]
    ordered = _topo_sort(gatherers)

    areas, _ = load_contexts(root)
    by_kind: Counter = Counter()
    areas_touched: set[str] = set()
    errors: list[str] = []
    total_added = 0

    for g in ordered:
        try:
            artifacts = await asyncio.wait_for(
                g.gather(root, since, cursor),
                timeout=g.timeout_seconds,
            )
        except asyncio.TimeoutError:
            log_error(root, g.name, f"timeout after {g.timeout_seconds}s")
            errors.append(f"{g.name}: timeout")
            continue
        except Exception as e:
            log_error(root, g.name, f"failed: {e!r}")
            errors.append(f"{g.name}: {e!r}")
            continue

        for art in artifacts:
            try:
                if _persist(root, art):
                    by_kind[art.kind] += 1
                    total_added += 1
                    # Map files_changed (or path) to areas for dirty-marking.
                    sig = art.signals or {}
                    paths: list[str] = []
                    if isinstance(sig.get("files_changed"), list):
                        paths.extend(p for p in sig["files_changed"] if isinstance(p, str))
                    if isinstance(sig.get("path"), str):
                        paths.append(sig["path"])
                    if art.area:
                        areas_touched.add(art.area)
                    if paths:
                        areas_touched.update(areas_for_paths(paths, areas))
            except Exception as e:
                log_error(root, g.name, f"persist failed for {art.id}: {e!r}")
                errors.append(f"{g.name}/{art.id}: persist failed")

    now = datetime.now(tz=timezone.utc)
    cursor["last_run_ts"] = now.isoformat()
    write_cursor(root, cursor)
    if areas_touched:
        mark_dirty(root, areas_touched)

    return GatherResult(
        artifacts_added=total_added,
        by_kind=dict(by_kind),
        areas_touched=tuple(sorted(areas_touched)),
        cursor_advanced_to=now,
        errors=tuple(errors),
    )
