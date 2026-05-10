"""
compact/runner.py — read every curated/*.md, ask the LLM to lint, write back.

For each curated file:
  1. Read its current content.
  2. Build a prompt: "here's the current page; tidy it per these rules".
  3. Call the LLM.
  4. If response is the NO-CHANGE sentinel, skip.
  5. Else, atomic-write the response over the file.

A side log of skipped/changed entries lands in metrics/ (compact_log.jsonl)
so the human can audit what the lint pass did.

Idempotent: running compact twice in a row should produce no second-pass
changes (the LLM should return NO-CHANGE the second time).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...models import Model
from ...provider import LLMProvider
from ..storage import curated_dir, metrics_dir, skills_dir
from .._llm import one_shot


# ── Built-in skill ────────────────────────────────────────────────────────────

DEFAULT_SKILL = """\
You are the wiki compactor. Your job is to lint a single curated wiki page
and produce a cleaner version. You are conservative: leave content alone
unless one of these rules clearly applies.

Rules (apply in this order):

  1. **Merge** two bullets that say the same thing (semantically) into one.
  2. **Demote** any claim that is internally contradicted by another bullet
     on the page (move it to a `## Deprecated` section at the bottom with a
     one-line note: "(superseded by …)" ).
  3. **Drop** bullets older than 12 months whose only signal is "ts":
     hot-files lists go stale; ADRs do not — keep ADR references.
  4. **Tighten** prose: if a sentence has filler (in order to, basically,
     it should be noted that), remove the filler. Do not change meaning.
  5. **Preserve** every citation `(commit …)`, `(PR #…)`, `(note: …)` and
     every code path. Never invent new ones.
  6. **Preserve** the H1 header exactly.

OUTPUT:

  - The full revised markdown page, ready to overwrite the original.
  - First line is the same H1 the input had.
  - If you would make zero changes, output exactly:

        NO CHANGE

  No prose explanation. No diff. Either the full new page or the sentinel.
"""

_NO_CHANGE = "NO CHANGE"


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompactResult:
    rewrote: tuple[Path, ...] = ()
    unchanged: tuple[Path, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()


def load_skill(repo_root: Path | str) -> str:
    """Return user-customised skill if present, else DEFAULT_SKILL."""
    p = skills_dir(Path(repo_root)) / "compact.md"
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_SKILL


# ── Public API ────────────────────────────────────────────────────────────────

async def compact_wiki(
    repo_root: Path | str,
    provider: LLMProvider,
    model: Model,
    *,
    dry_run: bool = False,
    max_concurrent: int = 2,
) -> CompactResult:
    """Lint every curated/*.md page. Atomic per-file rewrite."""
    root = Path(repo_root)
    curated = curated_dir(root)
    if not curated.is_dir():
        return CompactResult()

    targets = sorted(curated.rglob("*.md"))
    if not targets:
        return CompactResult()

    skill = load_skill(root)
    rewrote: list[Path] = []
    unchanged: list[Path] = []
    errors: list[tuple[str, str]] = []

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(path: Path) -> None:
        async with sem:
            try:
                rel = path.relative_to(curated)
            except ValueError:
                rel = path
            if dry_run:
                unchanged.append(path)
                return
            try:
                changed = await _compact_file(path, provider, model, skill)
            except Exception as e:
                errors.append((str(rel), f"{type(e).__name__}: {e}"))
                return
            if changed:
                rewrote.append(path)
                _log_compact(root, rel, changed=True)
            else:
                unchanged.append(path)
                _log_compact(root, rel, changed=False)

    await asyncio.gather(*(_one(p) for p in targets))
    return CompactResult(
        rewrote=tuple(rewrote),
        unchanged=tuple(unchanged),
        errors=tuple(errors),
    )


# ── One-file compact ──────────────────────────────────────────────────────────

async def _compact_file(
    path: Path,
    provider: LLMProvider,
    model: Model,
    skill: str,
) -> bool:
    """Return True if file was rewritten, False if NO CHANGE."""
    original = path.read_text(encoding="utf-8")
    user = (
        f"Here is the current page. Apply the rules and either return the\n"
        f"revised page or the NO CHANGE sentinel.\n\n"
        f"---\n{original}\n---"
    )
    res = await one_shot(provider, model, skill, user, max_tokens=4096)
    if res.error:
        raise RuntimeError(res.error)
    text = (res.text or "").strip()
    if not text or _NO_CHANGE in text.splitlines()[0]:
        return False
    if text == original.strip():
        return False
    stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    final = f"<!-- compacted: {stamp} by wiki/compact -->\n{text}\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(final, encoding="utf-8")
    tmp.replace(path)
    return True


def _log_compact(repo_root: Path, rel: Path, *, changed: bool) -> None:
    """Append one line to metrics/compact_log.jsonl. Best-effort."""
    try:
        log = metrics_dir(repo_root) / "compact_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "path": str(rel),
            "changed": changed,
        }
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
