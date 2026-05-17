"""
agent_forge.wiki.maintain — detect stale areas and re-run gather on them.

Stage 6 of the compounding loop. Heuristic: an area is "stale" if commits
touching it since the last gather exceed ``threshold`` (default 10). When
something is stale, we re-run gather with ``since=last_gather_ts`` and clear
the dirty markers.

Public:
    detect_stale_areas(repo_root, *, threshold=10) -> list[(area, n_commits)]
    run_maintain(repo_root, *, threshold=10) -> MaintainResult
    MaintainResult — frozen dataclass with counts + errors
"""
from __future__ import annotations

from .runner import MaintainResult, detect_stale_areas, run_maintain

__all__ = ["MaintainResult", "detect_stale_areas", "run_maintain"]
