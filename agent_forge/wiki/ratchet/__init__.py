"""
agent_forge.wiki.ratchet — promote per-session insights into raw/notes/.

Stage 4 of the compounding loop. Reads the session JSONL, asks the LLM
"what's worth remembering from this session?", writes a small markdown file
to ``.agent-forge/raw/notes/session/<sid>.md``. The next ``wiki gather`` run
picks it up via NotesGatherer — closing the chat → wiki feedback loop.

Skill-first: the prompt lives in ``.agent-forge/skills/ratchet.md`` (with
a built-in fallback). The Python here is just a harness: load the skill,
build the bundle, call the LLM, write the file.

Public:  ratchet_session(repo_root, session_id, provider, model, *, dry_run=False)
         load_skill(repo_root) -> str
         DEFAULT_SKILL — fallback prompt baked into the package
"""
from __future__ import annotations

from .runner import (
    DEFAULT_SKILL, RatchetResult, load_skill, ratchet_session,
)

__all__ = ["DEFAULT_SKILL", "RatchetResult", "load_skill", "ratchet_session"]
