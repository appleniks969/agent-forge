"""front/memory: the /remember write path — append a learning to project memory.

Layer: front — file I/O is allowed here (front is the only layer below wiring
that may touch disk besides adapters). This is the WRITE half of memory; the
READ half (merged global+project memory.md surfaced into the prompt) lives in
front/orient.py's memory_supplier. They share the same on-disk format so a
learning written here shows up in the next prompt build.

Format: project memory is <cwd>/.agent-forge/memory.md. Each learning is a
bullet line stamped `(learned YYYY-MM-DD)`. Appends dedup by a ~60-char prefix
(case-insensitive) so re-remembering the same fact is a no-op, and the file is
capped at ~2K tokens (~8KB) by evicting the oldest bullets first.
"""

from __future__ import annotations

import datetime
from pathlib import Path

_MEMORY_CAP_TOKENS = 2000
_DEDUP_PREFIX = 60
_HEADER = "## Memory\n"


def memory_path(cwd: Path) -> Path:
    """Project memory file: <cwd>/.agent-forge/memory.md (parents created)."""
    p = cwd / ".agent-forge" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def remember(cwd: Path, text: str) -> str:
    """Append a learning to project memory; return a one-line confirmation.

    Deduplicates against existing content by a case-insensitive ~60-char prefix
    of the stamped entry, and caps the file at ~2K tokens by evicting the oldest
    bullets first. Empty/blank text is rejected without touching the file.
    """
    learning = text.strip()
    if not learning:
        return "remember: nothing to remember (empty text)"

    p = memory_path(cwd)
    existing = p.read_text(encoding="utf-8") if p.exists() else _HEADER

    today = datetime.date.today().isoformat()
    entry = f"- {learning} (learned {today})"
    prefix = entry[:_DEDUP_PREFIX].lower()
    if prefix in existing.lower():
        return "remember: already known (skipped duplicate)"

    updated = existing.rstrip() + "\n" + entry + "\n"

    # Token cap — evict the oldest bullet lines until under budget. Estimate
    # ~4 bytes/token (the same heuristic the read path and v1 used).
    while len(updated.encode()) // 4 > _MEMORY_CAP_TOKENS:
        lines = updated.splitlines()
        bullets = [i for i, line in enumerate(lines) if line.strip().startswith("-")]
        # Stop if evicting the oldest would drop the one we just added (only
        # bullet left) — never lose the new learning to the cap.
        if len(bullets) <= 1:
            break
        lines.pop(bullets[0])
        updated = "\n".join(lines) + "\n"

    p.write_text(updated, encoding="utf-8")
    return f"remembered: {learning}"
