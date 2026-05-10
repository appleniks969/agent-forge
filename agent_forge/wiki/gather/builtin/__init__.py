"""
gather/builtin/__init__.py — registry of built-in gatherer classes.

The discovery layer imports BUILTINS to seed the gatherer list. User
gatherers from .agent-forge/gatherers/ are appended after these. Order
within the list doesn't matter — discovery does a topological sort by
`runs_after`.
"""
from __future__ import annotations

from .code_markers import CodeMarkersGatherer
from .git_history import GitHistoryGatherer
from .hotspots import HotspotsGatherer
from .notes import NotesGatherer
from .prs import PRsGatherer
from .repo_files import RepoFilesGatherer

BUILTINS = (
    NotesGatherer,
    RepoFilesGatherer,
    CodeMarkersGatherer,
    GitHistoryGatherer,
    PRsGatherer,
    HotspotsGatherer,    # runs_after = ("prs", "git_history")
)

__all__ = ["BUILTINS"]
