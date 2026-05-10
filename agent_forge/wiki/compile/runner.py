"""
compile/runner.py — read raw/, ask the LLM for narrative, write curated/.

Orchestration flow per output file:

  1. Build a bundle (compile/bundle.py) — pre-ranked, pre-truncated JSON
  2. Format the user message: "produce <output_name>.md from this bundle"
  3. Call the LLM (one_shot)
  4. Validate (non-empty, no obvious garbage)
  5. Atomic write to .agent-forge/curated/<output_name>.md

We produce four kinds of output:

  - onboarding.md           one global "where to start" narrative
  - hotspots.md             ranked list of files under churn
  - adrs.md                 decisions worth knowing
  - per_area/<area>.md      one focused page per declared area in contexts.yaml

Skill-first: ``.agent-forge/skills/compile.md`` overrides DEFAULT_SKILL.
The skill is the load-bearing artifact; the runner is plumbing.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ...models import Model
from ...provider import LLMProvider
from ..storage import (
    curated_dir, load_contexts, skills_dir,
)
from .._llm import one_shot
from .bundle import build_compile_bundle


# ── Built-in skill ────────────────────────────────────────────────────────────

DEFAULT_SKILL = """\
You are the wiki compiler for an engineering team. Your job is to read a JSON
bundle of repository facts (commits, PRs, hotspots, ADRs, notes, session
insights) and produce ONE markdown page that an engineer can skim in 60
seconds.

Hard rules:

  1. **Be terse.** No preamble, no recap of the bundle, no "in this section
     we will…". Headers + bullets, almost no prose.
  2. **Cite sources** by id when you make a non-obvious claim:
     `(commit a1b2c3d)`, `(PR #423)`, `(note: redis-decision)`.
  3. **Skip nothing-burgers.** If the bundle has 14 trivial commits and 1
     interesting one, write about the 1.
  4. **Group by topic, not by source.** Don't have a "PRs" section and a
     "commits" section — have a "Recent payments work" section that draws
     from both.
  5. **Stay under {budget} bytes.** If you can't fit, prioritise the
     non-obvious + recent.
  6. **No invention.** If the bundle doesn't say it, don't write it. The
     wiki must never hallucinate.
  7. **Empty-bundle case:** if the bundle is mostly empty, output:

         # {output_name}

         _(insufficient signal — run `agent-forge wiki gather` first.)_

OUTPUT FORMAT:

  - The first line is exactly: `# {output_name_human}`
  - No HTML, no front-matter, no closing summary.
  - Markdown headers `##` for sections, `-` for bullets, `\\`backticks\\`` for
     paths/identifiers.
"""

# ── Output specs ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _OutputSpec:
    """One curated/ file to produce."""
    name: str           # e.g. "onboarding"
    human_name: str     # e.g. "Onboarding"
    area: str | None    # None = global; else per-area
    budget: int         # byte budget the LLM is told to fit under
    relpath: Path       # destination relative to curated/

    def output_path(self, curated: Path) -> Path:
        return curated / self.relpath


_GLOBAL_OUTPUTS = (
    _OutputSpec("onboarding", "Onboarding",   None, 4000, Path("onboarding.md")),
    _OutputSpec("hotspots",   "Hot files",    None, 3000, Path("hotspots.md")),
    _OutputSpec("adrs",       "Decisions",    None, 4000, Path("adrs.md")),
)


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompileResult:
    written: tuple[Path, ...] = ()
    skipped: tuple[str, ...] = ()           # spec names skipped (e.g. dry-run)
    errors: tuple[tuple[str, str], ...] = ()  # (spec_name, error)


# ── Public API ────────────────────────────────────────────────────────────────

def load_skill(repo_root: Path | str) -> str:
    """Return user-customised skill if present, else DEFAULT_SKILL."""
    p = skills_dir(Path(repo_root)) / "compile.md"
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_SKILL


async def compile_wiki(
    repo_root: Path | str,
    provider: LLMProvider,
    model: Model,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
    max_concurrent: int = 2,
) -> CompileResult:
    """Compile every output spec in parallel (capped) and write to curated/.

    ``only`` filters by spec name (e.g. ``["onboarding", "hotspots"]``).
    """
    root = Path(repo_root)
    curated = curated_dir(root)
    curated.mkdir(parents=True, exist_ok=True)

    # Build the full output list: globals + one per-area page.
    areas, _ = load_contexts(root)
    specs: list[_OutputSpec] = list(_GLOBAL_OUTPUTS)
    for area in sorted(areas):
        specs.append(_OutputSpec(
            name=f"area:{area}",
            human_name=f"Area: {area}",
            area=area,
            budget=3500,
            relpath=Path("per_area") / f"{area}.md",
        ))

    if only:
        wanted = set(only)
        specs = [s for s in specs if s.name in wanted]

    skill = load_skill(root)
    written: list[Path] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []

    sem = asyncio.Semaphore(max_concurrent)

    async def _run_one(spec: _OutputSpec) -> None:
        async with sem:
            if dry_run:
                skipped.append(spec.name)
                return
            try:
                path = await _compile_one(root, spec, provider, model, skill)
            except Exception as e:
                errors.append((spec.name, f"{type(e).__name__}: {e}"))
                return
            if path is None:
                errors.append((spec.name, "empty LLM response"))
                return
            written.append(path)

    await asyncio.gather(*(_run_one(s) for s in specs))

    return CompileResult(
        written=tuple(written),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


# ── One-output compile ───────────────────────────────────────────────────────

async def _compile_one(
    repo_root: Path,
    spec: _OutputSpec,
    provider: LLMProvider,
    model: Model,
    skill: str,
) -> Path | None:
    """Build the bundle for one spec, call the LLM, write the file."""
    bundle = build_compile_bundle(repo_root, area=spec.area)

    sys_prompt = skill.format(
        budget=spec.budget,
        output_name=spec.name,
        output_name_human=spec.human_name,
    )
    user = (
        f"Produce the `{spec.relpath}` page now.\n"
        f"Output name (use as the H1): {spec.human_name}\n"
        f"Byte budget: {spec.budget}.\n"
        f"Area filter: {spec.area or '(none — global page)'}\n\n"
        f"Bundle (JSON):\n```json\n{bundle}\n```"
    )

    res = await one_shot(provider, model, sys_prompt, user, max_tokens=4096)
    if res.error:
        raise RuntimeError(res.error)
    text = (res.text or "").strip()
    if not text:
        return None

    out = spec.output_path(curated_dir(repo_root))
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    final = f"<!-- compiled: {stamp} by wiki/compile -->\n{text}\n"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(final, encoding="utf-8")
    tmp.replace(out)
    return out
