"""
gather/builtin/notes.py — pull human-authored notes from .agent-forge/notes/.

Simplest possible gatherer: glob *.md, parse optional YAML front matter
for `area:` and `tags:`, copy the markdown verbatim to raw/notes/, emit one
Artifact per file. No subprocess, no network, no auth.

This gatherer doubles as the canonical example of the Gatherer contract —
any user writing their own gatherer in .agent-forge/gatherers/ should be
able to read this file and understand the pattern.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ...storage import notes_dir, write_note_raw
from ...types import Artifact, Gatherer, Source

# Front matter is optional and minimal: --- ... --- at the top of the file,
# parsed by a tiny key:value reader. We deliberately don't pull PyYAML for
# this — notes that need richer structure should become custom gatherers.

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Return (front_matter_dict, body_without_fm). Empty dict if no fm."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, object] = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            fm[key] = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, text[m.end():]


class NotesGatherer(Gatherer):
    name = "notes"

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        d = notes_dir(repo_root)
        if not d.exists():
            return []
        out: list[Artifact] = []
        for path in sorted(d.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, body = _parse_front_matter(text)
            stat = path.stat()
            ts = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            # Use stem as id; on every gather we re-emit (storage SHA-cache
            # dedups, but mtime-based ids would re-emit on every edit which is
            # what we want — the latest copy of the note wins).
            note_id = f"note-{path.stem}"
            title = path.stem.replace("-", " ").replace("_", " ").strip().capitalize()
            tags = fm.get("tags") or []
            area = fm.get("area") if isinstance(fm.get("area"), str) else None

            # Mirror the markdown into raw/notes/ so compile (MVP 3) can quote it.
            try:
                write_note_raw(repo_root, path.stem, body)
            except OSError:
                continue

            out.append(Artifact(
                id=note_id,
                kind="note",
                source=Source.NOTE,
                title=title,
                body=body,
                ts=ts,
                area=area,
                signals={"tags": list(tags) if isinstance(tags, list) else [tags], "path": str(path.relative_to(repo_root))},
            ))
        return out
