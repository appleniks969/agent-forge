"""
agent_forge.wiki.compact — periodic lint of curated/ to fight wiki rot.

Stage 5 of the compounding loop. Reads each curated/*.md, asks the LLM
to merge redundant bullets, demote contradicted claims, drop stale
references. Writes back atomically. Run monthly.

Skill-first: ``.agent-forge/skills/compact.md`` overrides DEFAULT_SKILL.

Public:  compact_wiki(repo_root, provider, model, *, dry_run=False)
         load_skill(repo_root) -> str
         DEFAULT_SKILL — baked-in prompt
"""
from __future__ import annotations

from .runner import (
    DEFAULT_SKILL, CompactResult, compact_wiki, load_skill,
)

__all__ = ["DEFAULT_SKILL", "CompactResult", "compact_wiki", "load_skill"]
