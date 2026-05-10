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

Skill-first: each output kind has its own sharpened prompt under
``assets/skills/<kind>.md``. Per-repo override at
``.agent-forge/skills/wiki-compile-<kind>.md`` takes precedence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_forge.models import Model
from agent_forge.provider import LLMProvider
from ..storage import (
    curated_dir, load_contexts, skills_dir,
)
from .._llm import one_shot
from .bundle import build_compile_bundle


# ── Built-in skill resolution ─────────────────────────────────────────────────

# Map output spec name → packaged skill file basename under assets/skills/.
# Per-area outputs (name starts with "area:") share one prompt file.
_SKILL_NAMES: dict[str, str] = {
    "onboarding": "onboarding",
    "hotspots":   "hotspots",
    "adrs":       "adrs",
}

# The packaged-skill directory lives at:
#   <skill-root>/assets/skills/<kind>.md
# Skill-root is 4 levels up from this file:
#   .claude/skills/agent-forge-wiki/scripts/wiki/compile/runner.py
#                                  └────────── 4 .parent calls
_PACKAGED_SKILLS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "skills"

# Last-resort fallback if neither override nor packaged file exists. Kept
# deliberately generic; per-output sharpening lives in assets/skills/*.md.
_FALLBACK_SKILL = """\
You are the wiki compiler for an engineering team. Read the JSON bundle of
repository facts and produce ONE markdown page that an engineer can skim
in 60 seconds. Be terse, cite sources by id, group by topic. The first
line is exactly: `# {output_name_human}`. Stay under {budget} bytes.
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

def _skill_kind_for(spec_name: str) -> str:
    """Map an _OutputSpec.name to a skill kind under assets/skills/."""
    if spec_name.startswith("area:"):
        return "per_area"
    return _SKILL_NAMES.get(spec_name, spec_name)


def load_skill(repo_root: Path | str, spec_name: str = "onboarding") -> str:
    """Resolve the compile skill for one output spec.

    Resolution order (first hit wins):
      1. Per-repo override at .agent-forge/skills/wiki-compile-<kind>.md
      2. Per-repo legacy at .agent-forge/skills/compile.md (single-skill,
         pre-extraction shape — kept for back-compat)
      3. Packaged at <skill>/assets/skills/<kind>.md
      4. Built-in _FALLBACK_SKILL

    ``spec_name`` is the _OutputSpec.name (e.g. "onboarding", "hotspots",
    "adrs", or "area:<name>" — the last maps to per_area).
    """
    kind = _skill_kind_for(spec_name)
    sdir = skills_dir(Path(repo_root))

    # 1. New-shape per-repo override.
    p = sdir / f"wiki-compile-{kind}.md"
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass

    # 2. Legacy single-skill per-repo override (pre-extraction; applies to all).
    legacy = sdir / "compile.md"
    if legacy.exists():
        try:
            text = legacy.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass

    # 3. Packaged sharpened skill.
    pkg = _PACKAGED_SKILLS_DIR / f"{kind}.md"
    if pkg.exists():
        try:
            text = pkg.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass

    # 4. Built-in fallback.
    return _FALLBACK_SKILL


# Back-compat alias: prior shape exposed a single DEFAULT_SKILL string.
# Eagerly load the onboarding prompt as that constant; the per-output
# sharpening happens inside load_skill().
def _read_default() -> str:
    pkg = _PACKAGED_SKILLS_DIR / "onboarding.md"
    if pkg.exists():
        try:
            return pkg.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return _FALLBACK_SKILL.strip()


DEFAULT_SKILL: str = _read_default()


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
                # Per-output skill — sharpened for the kind of page.
                skill = load_skill(root, spec.name)
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
        area_filter=spec.area or "(none — global page)",
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
