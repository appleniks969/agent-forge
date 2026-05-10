"""
wiki/types.py — shared value types for the wiki subsystem.

Zero internal dependencies. Every wiki layer (gather today; compile, present,
ratchet later) imports `Artifact` and friends from here. Lives at `wiki/`
root rather than `wiki/gather/` because future siblings (compile, ratchet,
present) will need the same types — wiki subfolders are peers, not a
hierarchy. They share `types.py` and `storage.py` only.

Owns: Source enum, Artifact (the unit of exchange), GatherResult,
      Gatherer base class (the user-extensibility contract).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


# ── Enums ─────────────────────────────────────────────────────────────────────

class Source(StrEnum):
    """Provenance of an artifact — drives trust weighting at compile time."""
    BUILTIN = "builtin"   # produced by a gatherer in agent_forge.wiki.gather.builtin
    CUSTOM  = "custom"    # produced by a user gatherer in .agent-forge/gatherers/
    NOTE    = "note"      # human-authored markdown from .agent-forge/notes/
    DERIVED = "derived"   # second-pass computation over other artifacts (hotspots)


# ── Artifact: the unit of exchange ────────────────────────────────────────────

@dataclass(frozen=True)
class Artifact:
    """One unit of raw data — a PR, commit, ADR, note, hotspot, custom output.

    All fields except ``id``, ``kind``, ``source``, ``title``, ``ts`` are
    optional. ``signals`` is the open-ended bag for gatherer-specific facts
    (``is_bugfix``, ``is_revert``, ``files_changed``, ``bugfix_count_30d``, …).
    Compile (MVP 3) reads ``signals`` to weight entries.

    ``id`` is what storage uses for SHA-cache dedup — must be stable across
    runs (e.g. ``pr-423`` not a random uuid).
    """
    id: str
    kind: str               # "pr" | "commit" | "revert" | "adr" | "incident"
                            # | "repo_file" | "code_marker" | "note" | "hotspot"
                            # | "<gatherer.name>" for custom
    source: Source
    title: str
    body: str
    ts: datetime
    area: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)


# ── Gather result (status reporting) ──────────────────────────────────────────

@dataclass(frozen=True)
class GatherResult:
    """Return value from `run_gather()` — for CLI status output and tests."""
    artifacts_added: int
    by_kind: dict[str, int]
    areas_touched: tuple[str, ...]
    cursor_advanced_to: datetime
    errors: tuple[str, ...] = ()


# ── Gatherer: the user-extensibility contract ─────────────────────────────────

class Gatherer:
    """Subclass to pull data from any source into ``raw/``.

    Drop the file in ``.agent-forge/gatherers/`` and discovery picks it up
    automatically — same protocol as built-ins, no privileged status.

    Attributes
    ----------
    name : str
        Subdirectory name under ``raw/custom/`` (for user gatherers) or under
        ``raw/`` (for built-ins, which choose their own kind subdirectory).
    runs_after : tuple[str, ...]
        Names of other gatherers that must complete before this one runs.
        Used by hotspots (which depends on prs + git_history). Discovery does
        a topological sort.
    timeout_seconds : int
        Per-gatherer wall-clock budget. Killed by ``asyncio.wait_for`` on
        overrun; the rest of the run continues.
    """
    name: str = ""
    runs_after: tuple[str, ...] = ()
    timeout_seconds: int = 60

    async def gather(
        self,
        repo_root: Path,
        since: datetime,
        cursor: dict,
    ) -> list[Artifact]:
        """Produce artifacts since ``since``. Idempotent: same input → same output.

        ``cursor`` is the global cursor dict; gatherers may read or write a
        gatherer-private subkey (e.g. ``cursor["prs"]["highest_seen"]``).
        Discovery persists the cursor after the run.
        """
        raise NotImplementedError
