"""
agent_forge.wiki — repository knowledge subsystem.

Each stage of the compounding loop is its own peer subpackage of ``gather/``,
all sharing the root-level ``types.py`` and ``storage.py``:

    gather/    — pull repo-derived signal into raw/
    compile/   — synthesise raw/ → curated/ via LLM
    present/   — render raw/curated/ → SystemPrompt section (no LLM)
    ratchet/   — distil session JSONL → raw/notes/session/
    compact/   — periodic lint of curated/ to fight wiki rot
    maintain/  — detect stale areas + re-gather them
    metrics/   — citation / override / staleness signal logs

Public surface (kept small on purpose):

    Artifact, Source, Gatherer, GatherResult        ← types
    run_gather                                       ← orchestrator entry point
    load_contexts, list_artifacts, raw_dir          ← storage helpers others may need
"""
from __future__ import annotations

from .types import Artifact, GatherResult, Gatherer, Source
from .storage import list_artifacts, load_contexts, raw_dir
from .gather import run_gather
from .present import build_wiki_section

# LLM-using stages (lazy-import surfaces; the modules import the SDK lazily)
from .ratchet import ratchet_session
from .compile import compile_wiki
from .compact import compact_wiki
from .maintain import detect_stale_areas, run_maintain
from .metrics import (
    record_citation, record_override, snapshot_staleness, summarise,
)

__all__ = [
    "Artifact", "Source", "Gatherer", "GatherResult",
    "run_gather", "load_contexts", "list_artifacts", "raw_dir",
    "build_wiki_section",
    "ratchet_session", "compile_wiki", "compact_wiki",
    "detect_stale_areas", "run_maintain",
    "record_citation", "record_override", "snapshot_staleness", "summarise",
]
