"""
agent_forge.wiki.metrics — append-only signal logs for the wiki feedback loop.

Three ungameable signals (borrowed from ratchet's design):

  - **citation rate**     did the agent cite something from curated/?
                          → if low, gather/compile is incomplete
  - **override rate**     did the user contradict a cited claim?
                          → curated/ has wrong info; rerun ratchet
  - **staleness lag**     time between last commit and last gather, per area
                          → gather is overdue; trigger maintain

Write-only by design. No analysis pipeline, no dashboards — just JSONL on
disk that any downstream tool (or ``agent-forge wiki status``) can summarise.

Public:
    record_citation(repo_root, *, session_id, turn, source_id, snippet)
    record_override(repo_root, *, session_id, turn, citation_id, user_correction)
    snapshot_staleness(repo_root) -> dict[area, days_lag]
    summarise(repo_root, *, last_n_days=14) -> MetricsSummary
"""
from __future__ import annotations

from .runner import (
    MetricsSummary,
    record_citation,
    record_override,
    snapshot_staleness,
    summarise,
)

__all__ = [
    "MetricsSummary",
    "record_citation",
    "record_override",
    "snapshot_staleness",
    "summarise",
]
