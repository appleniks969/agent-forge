"""
agent_forge.wiki.present — render gathered/curated knowledge into a system-prompt section.

Stage 3 of the compounding loop. Pure mechanical: read files from
``.agent-forge/curated/`` (preferred) or fall back to ``.agent-forge/raw/``,
trim each chunk to a per-section byte budget, glue with markdown headers,
return one string suitable for ``SystemPrompt.register(SectionName.WIKI, …)``.

No LLM call here — that lives in ``compile/``. Picking is heuristic
(top-N by recency / hotness / file mtime). Prompt-aware ranking is the
MVP-3 follow-up; the function signature is shaped so adding it later is
non-breaking (just one more optional kwarg, or a sibling ``ranker.py``).

Public:  build_wiki_section(repo_root, *, budget=8000, user_prompt=None) -> str | None
"""
from __future__ import annotations

from .runner import build_wiki_section

__all__ = ["build_wiki_section"]
