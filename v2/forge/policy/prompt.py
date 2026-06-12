"""Prompt assembly policy: section thunks, content-hash memoization, stability tags.

Layer: policy — pure and synchronous, imports kernel only. Sections are thunks
re-resolved at EVERY build, so /clear and /remember actually refresh and the
capture-by-value bug class is structurally gone. Memoization keys on a content
hash — never mtime — so non-file state and same-mtime edits invalidate
correctly; unchanged content yields the identical PromptSection object, which
is what lets adapters key cache placement on section identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from forge.kernel.types import Effects, PromptSection, Stability, ToolSpec

DEFAULT_IDENTITY = (
    "You are forge, a coding agent operating inside the user's workspace.\n"
    "\n"
    "Match effort to the ask:\n"
    "- If the user is asking a question rather than requesting changes, answer "
    "in prose; do not use tools to disambiguate intent.\n"
    "- For a single function or snippet, deliver exactly that: one file (or an "
    "inline answer) implementing only the requested API surface. No project "
    "scaffolding (package.json, installs, test frameworks, configs) and no "
    "extra methods, options, or test suites unless asked for or already "
    "present in the workspace. A careful read-through is verification enough.\n"
    "- For multi-file or correctness-critical work, or when tests are "
    "requested: verify before finishing — compile and run what you wrote, "
    "preferring the workspace's existing toolchain over installing one, and "
    "fix what fails.\n"
    "\n"
    "Working rules:\n"
    "- Inspect before you edit; prefer small, verifiable changes.\n"
    "- Use the file tools for reading and editing; use Bash for builds, "
    "tests, and git.\n"
    "- Confirm before destructive operations (rm -rf, git reset --hard, "
    "force-push); never commit or push unless explicitly asked.\n"
    "- Report what you did briefly and exactly; state plainly anything you "
    "did not verify."
)


@dataclass(frozen=True)
class SectionThunk:
    """A named, stability-tagged text producer. resolve() is re-called at every
    build; returning None or whitespace omits the section from that build."""

    name: str
    stability: Stability
    resolve: Callable[[], str | None]


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PromptAssembler:
    """Re-resolves every thunk per build; memoizes sections on content hash.

    The memo is observationally pure: equal content in, identical section
    object out. It never consults mtimes or resolve-call counts — only the
    digest of what the thunk returned this build.
    """

    def __init__(self, thunks: Iterable[SectionThunk]) -> None:
        self._thunks = tuple(thunks)
        self._memo: dict[str, tuple[str, PromptSection]] = {}

    @property
    def thunks(self) -> tuple[SectionThunk, ...]:
        return self._thunks

    def build(self) -> tuple[PromptSection, ...]:
        sections: list[PromptSection] = []
        for thunk in self._thunks:
            text = thunk.resolve()
            if text is None or not text.strip():
                continue
            digest = content_digest(text)
            hit = self._memo.get(thunk.name)
            if hit is not None and hit[0] == digest:
                sections.append(hit[1])
                continue
            section = PromptSection(
                name=thunk.name, text=text, stability=thunk.stability
            )
            self._memo[thunk.name] = (digest, section)
            sections.append(section)
        return tuple(sections)


# --- standard section builders --------------------------------------------------


def identity_section(text: str = DEFAULT_IDENTITY) -> SectionThunk:
    return SectionThunk("identity", Stability.STATIC, lambda: text)


def environment_section(
    facts: Callable[[], Mapping[str, str]],
) -> SectionThunk:
    """facts is a supplier (injected by the composition root — policy never
    reads os.environ) re-queried at every build; VOLATILE so adapters never
    cache it."""

    def resolve() -> str | None:
        items = facts()
        if not items:
            return None
        lines = "\n".join(f"{key}: {value}" for key, value in items.items())
        return f"# Environment\n{lines}"

    return SectionThunk("environment", Stability.VOLATILE, resolve)


def tools_section(specs: Callable[[], Sequence[ToolSpec]]) -> SectionThunk:
    """SESSION stability: the tool set is stable within a session but changes
    on MCP reconnect, which the re-resolved thunk picks up automatically."""

    def resolve() -> str | None:
        tool_specs = specs()
        if not tool_specs:
            return None
        return "# Tools\n" + "\n".join(_tool_line(s) for s in tool_specs)

    return SectionThunk("tools", Stability.SESSION, resolve)


def agents_doc_section(supplier: Callable[[], str | None]) -> SectionThunk:
    """Project instructions (AGENTS.md/CLAUDE.md). supplier is the I/O seam —
    injected by the composition root so policy never touches the filesystem."""

    return SectionThunk("agents_doc", Stability.SESSION, supplier)


def repo_map_section(supplier: Callable[[], str | None]) -> SectionThunk:
    """Git-recency-weighted repo tree. supplier is the injected I/O seam."""

    return SectionThunk("repo_map", Stability.SESSION, supplier)


def memory_section(supplier: Callable[[], str | None]) -> SectionThunk:
    """Merged global+project memory. supplier is the injected I/O seam."""

    return SectionThunk("memory", Stability.SESSION, supplier)


def skills_section(supplier: Callable[[], str | None]) -> SectionThunk:
    """Skill catalog index. supplier is the injected I/O seam."""

    return SectionThunk("skills", Stability.SESSION, supplier)


def _tool_line(spec: ToolSpec) -> str:
    desc = " ".join(spec.description.split())
    label = _effects_label(spec.effects)
    suffix = f" [{label}]" if label else ""
    return f"- {spec.name}: {desc}{suffix}"


def _effects_label(effects: Effects) -> str:
    if not effects:
        return ""
    return "|".join(
        flag.name.lower() for flag in Effects if flag in effects and flag.name
    )
