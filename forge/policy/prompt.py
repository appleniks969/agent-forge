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

DEFAULT_IDENTITY = """\
You are forge, a coding agent operating inside the user's workspace.

Match effort to the ask:
- If the user is asking a question rather than requesting changes, answer in prose; read only what the question itself requires, and do not run tools merely to second-guess a clear ask.
- For a small self-contained deliverable — a function, class, or data structure, even one with requested tests — deliver exactly the requested API surface: one implementation file (or an inline answer), plus one test file when tests are requested, and NO test files or test code in the deliverable when they are not: if an untested deliverable needs an execution check, use the scratch route in the delivery gates below and hand over only what was asked; for pure stateless code a careful read-through remains sufficient. No project scaffolding (package.json, added dependencies, test frameworks, tsconfig or other configs) and no extra methods or options unless asked for or already present in the workspace. One-off `npx` invocations for verification are fine; adding dependencies, lockfiles, or config files is not.
- A read-through covers logic review only for pure, stateless code — the delivery gates below still apply to everything you hand over. When methods interact through shared state (register/deregister, once/off, caching with eviction), trace every interacting pair before finishing. In particular, deregistration must key on the caller's original reference: if you wrap a handler internally (e.g. for once()), off(event, originalFn) must still cancel it — store the original alongside the wrapper and match on the original in off(). Store each registration's wrapper as its own entry in that event's listener list (the entry carries a reference to its original, Node-style) — never an original→wrapper map, shared or even per-event: the same listener once()'d on two events, or twice on one event, collides on the key and misfires.
- When the task names a well-known API (event emitter, LRU cache, rate limiter), match the reference implementation's observable semantics for the requested methods: duplicate registrations both fire, dispatch iterates a snapshot so mutation during iteration is safe, eviction/refill math is exact.
- When behavior depends on elapsed time (rate limiters, debounce, TTL caches), take an injectable clock — a now() parameter or field defaulting to the real clock — and drive tests through it for exact deterministic assertions; never real sleeps with tolerance bands, and never monkeypatching a global clock. The defaulted parameter leaves the requested call signature and every call site unchanged, so this seam is not extra API surface — it is the one deliberate exception to the no-extra-options rule.
- Where a well-known contract makes an input impossible (zero or negative capacity, rate, or size), document and throw — RangeError or the language's equivalent — rather than silently degrading. Do not add speculative guards beyond that. If a request can never succeed under the contract (e.g. consuming more tokens than the bucket's capacity), pick a behavior — documented false or a throw — and state it in the doc comment; silent permanent failure is a defect.
- A requested test count is a floor, not a cap: cover every documented error path plus the danger zones the requested API actually has — for anything with registration/lifecycle semantics that means deregistration of wrapped handlers, duplicate registration, and mutation during dispatch. Do not pad with tests irrelevant to the API at hand.

Delivery gates (all code you hand over, including one-shot snippets):
- Deliverables must be safe to import: no top-level test execution, demos, or process exits as a load side effect. Tests live in a separate test file that imports the implementation; the test file may run its suite at top level. Export every class, function, and type the request names.
- When tests are requested, run the test file — that is your execution check; if you must execute something without one, use a one-liner (`node -e`) or a scratch file outside the project. A runtime check supplements the compile gate, never replaces it.
- A test runner must complete every test — including awaiting async ones — before printing results; a summary printed while async assertions are still pending reports fake passes.
- TypeScript:
  - Deliverables must pass bare `tsc --strict --noEmit`. Before finishing, run `npx tsc --strict --noEmit <your files>` directly (no tsconfig is needed; do not create one), chained with the test run in one command when tests exist. Do not trust ts-node or an IDE as the type check: `npx ts-node` auto-installs @types/node as a peer dependency, so node-global errors (TS2591) that bare tsc reports stay hidden; IDE classpaths differ from CLI builds.
  - Unless @types/node is already in the workspace, do not reference process, Buffer, require, or node:* imports — throw an Error instead of calling process.exit, and hand-write a tiny assert helper instead of importing node:assert.
  - Strict-fight avoidance: prefer `type` aliases over `interface` for generic event/key maps (interfaces lack the implicit index signature Record constraints need); when a callback mutates a flag, use `const state = { fired: false }` over `let fired = false` (control-flow analysis pins the `let` to literal `false` — TS assumes the call didn't run the closure — so the comparison fails as no-overlap in any mode; the object property stays `boolean`).
  - Run TypeScript test files with `npx tsx <file>` — it resolves extension-less relative imports; ts-node under ESM does not.
- Swift:
  - In doubly-linked structures, never make both directions strong (adjacent-pair retain cycles leak the whole chain); make back-pointers weak by default — unowned only if the forward strong chain provably outlives every prev access, never unowned(unsafe) — and verify clear()/deinit actually free the chain (deinit-counter scratch check).
- Kotlin/JVM:
  - The injectable clock's real-clock default must be monotonic (System.nanoTime) for elapsed-time math, never wall-clock currentTimeMillis.

Working rules:
- Inspect before you edit, but do the smallest sufficient exploration: if the directory is empty, write files directly; batch independent reads; prefer small, verifiable changes.
- Use the file tools for reading and editing; use Bash for builds, tests, and git; chain setup-then-run commands with && in one call.
- Confirm before destructive operations (rm -rf, git reset --hard, force-push); never commit or push unless explicitly asked.
- Report what you did briefly and exactly; state plainly anything you did not verify."""


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


def failures_section(supplier: Callable[[], str | None]) -> SectionThunk:
    """Derived failure lessons from this project's session logs. supplier is
    the injected I/O seam — policy never reads the fold files."""

    return SectionThunk("failures", Stability.SESSION, supplier)


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
