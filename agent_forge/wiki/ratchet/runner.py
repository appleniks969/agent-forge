"""
ratchet/runner.py — read a session, distill it, write to raw/notes/session/.

The whole job is:

  1. Load the session's transcript (session.resume_session)
  2. Render it into a compact bundle (bundle.py)
  3. Load the skill prompt (.agent-forge/skills/ratchet.md or DEFAULT_SKILL)
  4. Call the LLM (one_shot)
  5. Validate the output ("NOTHING TO RATCHET" sentinel == empty file)
  6. Write to .agent-forge/raw/notes/session/<sid>.md

Idempotency: re-running on the same session overwrites the same file. The
output is a *current snapshot of insights*, not an append log — sessions
that have already been summarised will produce the same/similar output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...models import Model
from ...provider import LLMProvider
from ..storage import skills_dir, write_session_insight
from .._llm import one_shot
from .bundle import build_session_bundle


# ── Built-in skill (fallback when .agent-forge/skills/ratchet.md is absent) ──

DEFAULT_SKILL = """\
You read a chat-session transcript between a human engineer and an AI coding
agent, and extract durable insights worth promoting into the repository's
long-term knowledge wiki.

You are NOT a summariser. Most sessions contain nothing worth keeping. Your
default answer is "NOTHING TO RATCHET". Only emit insights when they meet
ALL of these criteria:

  1. **Repository-specific** — about *this* codebase, not general knowledge.
     "use a transaction for migrations" — yes. "Python lists are mutable" — no.
  2. **Durable** — likely to still be true in 6 months.
  3. **Non-obvious** — would a new engineer be surprised? if obvious, skip.
  4. **Confirmed** — the human acknowledged it, or the agent verified it
     against the codebase. Speculation does NOT qualify.

OUTPUT FORMAT (markdown, no preamble):

    # Session insights

    - One short, declarative sentence per insight.
    - Cite the file or PR if relevant: `src/refund.py`, PR #441.
    - Maximum 5 bullets. Fewer is better.
    - Skip code blocks unless a one-liner pattern is the insight itself.

If nothing qualifies, output exactly:

    NOTHING TO RATCHET

No other text. No explanation. The downstream gather pipeline treats this
sentinel as "delete the file".
"""

_SENTINEL = "NOTHING TO RATCHET"


@dataclass(frozen=True)
class RatchetResult:
    session_id: str
    wrote: bool                  # False if sentinel returned (or error)
    output_path: Path | None
    insights_text: str
    error: str | None = None


def load_skill(repo_root: Path | str) -> str:
    """Return user-customised skill if present, else DEFAULT_SKILL."""
    p = skills_dir(Path(repo_root)) / "ratchet.md"
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_SKILL


async def ratchet_session(
    repo_root: Path | str,
    session_id: str,
    provider: LLMProvider,
    model: Model,
    *,
    dry_run: bool = False,
    max_bundle_chars: int = 24_000,
) -> RatchetResult:
    """Distill a session and write insights to raw/notes/session/<sid>.md.

    Returns a RatchetResult. If the LLM returns the NOTHING TO RATCHET
    sentinel, no file is written and ``wrote=False``.
    """
    root = Path(repo_root)
    bundle = build_session_bundle(session_id, max_chars=max_bundle_chars)
    if not bundle or not bundle.strip():
        return RatchetResult(
            session_id=session_id, wrote=False, output_path=None,
            insights_text="", error="empty session",
        )

    skill = load_skill(root)
    user = (
        "Here is the session transcript. Apply the rules above and produce "
        "either the markdown insights or the NOTHING TO RATCHET sentinel.\n\n"
        f"---\n{bundle}\n---"
    )

    if dry_run:
        return RatchetResult(
            session_id=session_id, wrote=False, output_path=None,
            insights_text=f"[dry-run] would call LLM; bundle={len(bundle)} chars",
        )

    res = await one_shot(provider, model, skill, user, max_tokens=2048)
    if res.error:
        return RatchetResult(
            session_id=session_id, wrote=False, output_path=None,
            insights_text="", error=res.error,
        )

    text = (res.text or "").strip()
    if not text or _SENTINEL in text.splitlines()[0]:
        return RatchetResult(
            session_id=session_id, wrote=False, output_path=None,
            insights_text=text,
        )

    # Prepend a small provenance header so the gather pipeline (and humans)
    # know where this file came from.
    stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    final = (
        f"---\nsource: ratchet\nsession_id: {session_id}\ngenerated: {stamp}\n---\n\n"
        f"{text}\n"
    )
    out = write_session_insight(root, session_id, final)
    return RatchetResult(
        session_id=session_id, wrote=True, output_path=out,
        insights_text=text,
    )
