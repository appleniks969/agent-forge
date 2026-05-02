"""
system_prompt.py — SystemPrompt aggregate + SectionName ordered enum.

Depends only on provider (SystemPromptSection). Lives as a sibling to
context.py — both are leaves above provider but they manage unrelated
concerns: ContextWindow handles message-history pressure and eviction,
SystemPrompt handles ordered prompt sections with Anthropic ephemeral cache
placement. They were combined historically because both were "things one
step above provider"; that's a file-size heuristic, not a domain boundary.

Owns: SectionName (StrEnum with .order, .cache_group, .is_volatile),
      SystemPrompt (register / register_extra / build / invalidate_*),
      _Section (lazy-resolve cache for non-volatile sections).

Caching policy: cache_control=True is placed on the last non-null section
of each cache group (groups 0/1/2 cached; group 3 volatile, never cached).
Plugin-contributed extras (register_extra) are appended after named sections
and never cached.
"""
from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field

from .provider import SystemPromptSection


class SectionName(enum.StrEnum):
    """Stable ordering enforced at the type level."""
    IDENTITY    = "identity"     # group 0 (STABLE)
    TOOLS       = "tools"        # group 0
    GUIDELINES  = "guidelines"   # group 0
    AGENTS_DOC  = "agents_doc"   # group 1 (SESSION-STABLE)
    SKILLS      = "skills"       # group 1
    MEMORY      = "memory"       # group 1
    REPO_MAP    = "repo_map"     # group 2 (SESSION-STABLE, separate breakpoint)
    ENVIRONMENT = "environment"  # group 3 (VOLATILE)
    CUSTOM      = "custom"       # group 3

    @property
    def order(self) -> int:
        return {
            "identity": 0, "tools": 1, "guidelines": 2,
            "agents_doc": 3, "skills": 4, "memory": 5,
            "repo_map": 6, "environment": 7, "custom": 8,
        }[self.value]

    @property
    def cache_group(self) -> int:
        if self in (SectionName.IDENTITY, SectionName.TOOLS, SectionName.GUIDELINES): return 0
        if self in (SectionName.AGENTS_DOC, SectionName.SKILLS, SectionName.MEMORY): return 1
        if self == SectionName.REPO_MAP: return 2
        return 3  # VOLATILE

    @property
    def is_volatile(self) -> bool:
        return self in (SectionName.ENVIRONMENT, SectionName.CUSTOM)


@dataclass
class _Section:
    name: SectionName
    compute: Callable[[], str | None]
    _cached: str | None = field(default=None, init=False)
    _computed: bool = field(default=False, init=False)

    def resolve(self) -> str | None:
        if self.name.is_volatile:
            return self.compute()
        if not self._computed:
            self._cached = self.compute()
            self._computed = True
        return self._cached

    def invalidate(self) -> None:
        self._cached = None
        self._computed = False


class SystemPrompt:
    """
    Aggregate: ordered system prompt sections with Anthropic ephemeral cache support.

    Policy: cache_control=True on the last non-null section of each cache group.
    """

    def __init__(self) -> None:
        self._sections: dict[SectionName, _Section] = {}
        # Plugin-contributed extra sections (appended after named sections, no caching)
        self._extra_sections: list[tuple[str, Callable[[], str | None]]] = []

    def register(self, name: SectionName, compute: Callable[[], str | None]) -> None:
        self._sections[name] = _Section(name=name, compute=compute)

    def register_extra(
        self,
        name: str,
        compute: Callable[[], str | None],
    ) -> None:
        """
        Register a plugin-contributed section.

        Sections added here are appended AFTER all named (SectionName) sections
        in the order they were registered.  They are always volatile (cache_group 3
        — never cached) so they do not interfere with Anthropic prompt-caching.
        """
        self._extra_sections.append((name, compute))

    def build(self) -> list[SystemPromptSection]:
        """
        Resolve all sections in stable order.
        Places cache_control=True on the last non-null section of each group.
        Groups: 0 (stable), 1 (session-stable), 2 (repo_map), 3 (volatile — no cache).
        """
        # Resolve all sections in order
        resolved: list[tuple[SectionName, str]] = []
        for name in sorted(self._sections, key=lambda n: n.order):
            value = self._sections[name].resolve()
            if value and value.strip():
                resolved.append((name, value))

        if not resolved:
            # Still process extras even when no named sections resolved
            extras_only: list[SystemPromptSection] = []
            for _ename, efn in self._extra_sections:
                try:
                    ev = efn()
                except Exception:
                    ev = None
                if ev and ev.strip():
                    extras_only.append(SystemPromptSection(text=ev, cache_control=False))
            return extras_only

        # Find last index per cache group (groups 0-2 get cache_control)
        last_in_group: dict[int, int] = {}
        for i, (name, _) in enumerate(resolved):
            g = name.cache_group
            if g < 3:  # don't cache volatile group
                last_in_group[g] = i

        result: list[SystemPromptSection] = []
        for i, (name, text) in enumerate(resolved):
            cache = (name.cache_group < 3 and last_in_group.get(name.cache_group) == i)
            result.append(SystemPromptSection(text=text, cache_control=cache))

        # Append plugin extra sections — always volatile, never cached
        for _ename, efn in self._extra_sections:
            try:
                ev = efn()
            except Exception:
                ev = None
            if ev and ev.strip():
                result.append(SystemPromptSection(text=ev, cache_control=False))

        return result

    def invalidate_session(self) -> None:
        """Invalidate session-stable sections (groups 1+2). Called on /clear."""
        session_groups = {SectionName.AGENTS_DOC, SectionName.SKILLS, SectionName.MEMORY, SectionName.REPO_MAP}
        for name, sec in self._sections.items():
            if name in session_groups:
                sec.invalidate()

    def invalidate_all(self) -> None:
        for sec in self._sections.values():
            sec.invalidate()
        # Extra sections use lambdas (called on every build), so nothing to invalidate
