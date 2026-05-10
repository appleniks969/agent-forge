"""
agent_forge.wiki.compile — synthesise raw/ artifacts into curated/ narratives.

Stage 2 of the compounding loop. Reads ``.agent-forge/raw/cache/`` (+ any
ratchet'd session insights under ``raw/notes/session/``), asks the LLM to
group/rank/dedupe them per area, and writes:

    curated/onboarding.md       (top-level "where do I start?" prose)
    curated/hotspots.md          (ranked list of files under churn)
    curated/adrs.md              (decisions worth knowing)
    curated/per_area/<area>.md  (one focused page per declared area)

Skill-first: the prompt lives in ``.agent-forge/skills/compile.md`` (with
a built-in fallback). The runner is ~150 LOC of bundle-prep + LLM call +
file write.

Public:  compile_wiki(repo_root, provider, model, *, only=None, dry_run=False)
         load_skill(repo_root) -> str
         DEFAULT_SKILL — the baked-in prompt
"""
from __future__ import annotations

from .runner import (
    DEFAULT_SKILL, CompileResult, compile_wiki, load_skill,
)

__all__ = ["DEFAULT_SKILL", "CompileResult", "compile_wiki", "load_skill"]
