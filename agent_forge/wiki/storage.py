"""
wiki/storage.py — the I/O layer for the wiki subsystem.

Everything that touches disk under ``.agent-forge/`` goes through here.
No business logic, no LLM calls, no shellouts — just paths, atomic writes,
the SHA-dedup cache, the gather cursor, the dirty-area markers, and the
``contexts.yaml`` reader.

Lives at ``wiki/`` root because both gather and the future
compile/present/ratchet layers need it. Subfolders depend on storage but
not on each other.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .types import Artifact, Source

# ── Paths ─────────────────────────────────────────────────────────────────────

def wiki_root(repo_root: Path | str) -> Path:
    """Return ``<repo_root>/.agent-forge``. Does not create it."""
    return Path(repo_root) / ".agent-forge"


def raw_dir(repo_root: Path | str) -> Path:
    """Compatibility alias — historical "raw root". Now points at ``raw/cache/``
    so existing callers (gather, status) continue to land artifacts in the
    regenerable cache layer. ``raw/notes/`` is for human/ratchet writes only.
    """
    return wiki_root(repo_root) / "raw" / "cache"


def raw_root(repo_root: Path | str) -> Path:
    """The full ``.agent-forge/raw/`` directory (parent of cache/ + notes/)."""
    return wiki_root(repo_root) / "raw"


def raw_cache_dir(repo_root: Path | str) -> Path:
    """Regenerable extracted facts (commits, PRs, hotspots, …). Safe to delete."""
    return wiki_root(repo_root) / "raw" / "cache"


def raw_notes_dir(repo_root: Path | str) -> Path:
    """Sacred: human notes + ratchet session insights. Never deleted automatically."""
    return wiki_root(repo_root) / "raw" / "notes"


def notes_dir(repo_root: Path | str) -> Path:
    """User-authored notes input directory. Read by NotesGatherer.

    Lives at the top level (``.agent-forge/notes/``) so a hand-written note
    is just ``.agent-forge/notes/payments.md`` — no nested path. The gatherer
    mirrors them into ``raw/notes/manual/`` for downstream consumers.
    """
    return wiki_root(repo_root) / "notes"


def curated_dir(repo_root: Path | str) -> Path:
    """LLM-compiled views of raw/ (MVP-3 compile target)."""
    return wiki_root(repo_root) / "curated"


def skills_dir(repo_root: Path | str) -> Path:
    """Markdown prompts that drive the LLM-using stages (compile, ratchet, compact)."""
    return wiki_root(repo_root) / "skills"


def metrics_dir(repo_root: Path | str) -> Path:
    """Append-only signal logs for the wiki feedback loop (MVP-3.5)."""
    return wiki_root(repo_root) / "metrics"


def gatherers_dir(repo_root: Path | str) -> Path:
    return wiki_root(repo_root) / "gatherers"


def contexts_path(repo_root: Path | str) -> Path:
    return wiki_root(repo_root) / "contexts.yaml"


def cursor_path(repo_root: Path | str) -> Path:
    return raw_cache_dir(repo_root) / ".cursor"


def sha_cache_path(repo_root: Path | str) -> Path:
    return raw_cache_dir(repo_root) / ".cache" / "sha.txt"


def dirty_path(repo_root: Path | str) -> Path:
    return raw_cache_dir(repo_root) / ".dirty"


def gather_log_path(repo_root: Path | str) -> Path:
    return raw_cache_dir(repo_root) / ".gather.log"


def ensure_layout(repo_root: Path | str) -> None:
    """Create ``.agent-forge/raw/cache/`` + ``raw/notes/`` if missing. Idempotent."""
    cache = raw_cache_dir(repo_root)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / ".cache").mkdir(parents=True, exist_ok=True)
    raw_notes_dir(repo_root).mkdir(parents=True, exist_ok=True)


# ── Atomic write ──────────────────────────────────────────────────────────────

def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (write to .tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_append_line(path: Path, line: str) -> None:
    """Append a single line. Not atomic across writers, but fine for one process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


# ── Artifact I/O ──────────────────────────────────────────────────────────────

_KIND_DIRS: dict[str, str] = {
    "pr":           "prs",
    "commit":       "commits",
    "revert":       "reverts",
    "adr":          "adrs",
    "incident":     "incidents",
    "repo_file":    "repo_files",
    "code_marker":  "code_markers",
    "note":         "notes",
    "hotspot":      "hotspots",
}

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(id_: str) -> str:
    """Convert an arbitrary artifact id to a filesystem-safe name."""
    return _SAFE_ID_RE.sub("-", id_).strip("-") or "unknown"


def artifact_path(repo_root: Path | str, art: Artifact) -> Path:
    """Resolve where an artifact is written. Custom kinds land under raw/custom/<name>/."""
    if art.source == Source.CUSTOM:
        # Custom gatherers land under raw/cache/custom/<gatherer-name>/<id>.json.
        # The kind doubles as the gatherer name for custom gatherers.
        sub = Path("custom") / _safe_filename(art.kind)
    else:
        sub = Path(_KIND_DIRS.get(art.kind, art.kind))
    return raw_cache_dir(repo_root) / sub / f"{_safe_filename(art.id)}.json"


def _artifact_to_dict(art: Artifact) -> dict:
    d = asdict(art)
    d["source"] = art.source.value
    d["ts"] = art.ts.isoformat()
    return d


def _dict_to_artifact(d: dict) -> Artifact:
    return Artifact(
        id=d["id"],
        kind=d["kind"],
        source=Source(d["source"]),
        title=d["title"],
        body=d["body"],
        ts=datetime.fromisoformat(d["ts"]),
        area=d.get("area"),
        signals=d.get("signals") or {},
    )


def write_artifact(repo_root: Path | str, art: Artifact) -> Path:
    """Atomically write an artifact to its kind-specific subdirectory."""
    path = artifact_path(repo_root, art)
    _atomic_write_text(path, json.dumps(_artifact_to_dict(art), indent=2))
    return path


def read_artifact(path: Path) -> Artifact:
    """Read a single artifact JSON file."""
    return _dict_to_artifact(json.loads(path.read_text(encoding="utf-8")))


def list_artifacts(
    repo_root: Path | str,
    kind: str | None = None,
) -> Iterator[Artifact]:
    """Iterate over artifacts under raw/cache/. If kind given, only that subdirectory."""
    root = raw_cache_dir(repo_root)
    if not root.exists():
        return
    if kind is not None:
        sub = root / _KIND_DIRS.get(kind, kind)
        if not sub.exists():
            return
        candidates = sorted(sub.glob("*.json"))
    else:
        # Walk every subdir, skipping cache/log files at root.
        candidates = sorted(p for p in root.rglob("*.json") if ".cache" not in p.parts)
    for p in candidates:
        try:
            yield read_artifact(p)
        except (json.JSONDecodeError, KeyError, ValueError):
            # Tolerate corrupt files — gather will overwrite on next run.
            continue


# ── Notes (raw markdown copy) ─────────────────────────────────────────────────

def write_note_raw(repo_root: Path | str, name: str, content: str) -> Path:
    """Mirror a hand-authored note's body into raw/notes/manual/.

    The Artifact JSON sibling lives under raw/cache/notes/. raw/notes/manual/
    is the *truly raw* copy: human-authored markdown that survives
    `rm -rf raw/cache/`.
    """
    path = raw_notes_dir(repo_root) / "manual" / f"{_safe_filename(name)}.md"
    _atomic_write_text(path, content)
    return path


def write_session_insight(repo_root: Path | str, session_id: str, content: str) -> Path:
    """Write a ratchet-extracted session insight to raw/notes/session/<sid>.md.

    These files are read by NotesGatherer (via the same notes/ pipeline) on
    the next `wiki gather` — closing the loop from chat → wiki.
    """
    path = raw_notes_dir(repo_root) / "session" / f"{_safe_filename(session_id)}.md"
    _atomic_write_text(path, content)
    return path


# ── Cursor ────────────────────────────────────────────────────────────────────

def read_cursor(repo_root: Path | str) -> dict:
    """Return the persisted cursor dict, or {} if missing/corrupt."""
    p = cursor_path(repo_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_cursor(repo_root: Path | str, cursor: dict) -> None:
    """Persist the cursor dict atomically."""
    _atomic_write_text(cursor_path(repo_root), json.dumps(cursor, indent=2, default=str))


# ── SHA cache (idempotent dedup) ──────────────────────────────────────────────

def _load_sha_cache(repo_root: Path | str) -> set[str]:
    p = sha_cache_path(repo_root)
    if not p.exists():
        return set()
    try:
        return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def sha_seen(repo_root: Path | str, sha: str) -> bool:
    """O(1)-after-load check. Cache loaded lazily on first call."""
    return sha in _load_sha_cache(repo_root)


def sha_record(repo_root: Path | str, sha: str) -> None:
    """Append a sha to the cache. Idempotent at the caller; we don't dedup writes."""
    _atomic_append_line(sha_cache_path(repo_root), sha)


# ── Dirty area markers ────────────────────────────────────────────────────────

def read_dirty(repo_root: Path | str) -> set[str]:
    p = dirty_path(repo_root)
    if not p.exists():
        return set()
    try:
        return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def mark_dirty(repo_root: Path | str, areas: Iterable[str]) -> None:
    current = read_dirty(repo_root)
    new = current | {a for a in areas if a}
    if new == current:
        return
    _atomic_write_text(dirty_path(repo_root), "\n".join(sorted(new)) + "\n")


def clear_dirty(repo_root: Path | str, area: str) -> None:
    current = read_dirty(repo_root)
    if area not in current:
        return
    current.discard(area)
    _atomic_write_text(dirty_path(repo_root), "\n".join(sorted(current)) + ("\n" if current else ""))


# ── Gather log (for failures) ─────────────────────────────────────────────────

def log_error(repo_root: Path | str, gatherer: str, message: str) -> None:
    """Append an error from a single gatherer; never raises."""
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_append_line(gather_log_path(repo_root), f"{ts}\t{gatherer}\t{message}")
    except Exception:
        pass


# ── contexts.yaml loader ──────────────────────────────────────────────────────

def load_contexts(repo_root: Path | str) -> tuple[dict[str, list[str]], set[str]]:
    """Read .agent-forge/contexts.yaml, return (areas → glob list, inline_authors).

    Returns ({}, set()) if the file is missing — wiki gather still works
    without contexts.yaml; areas just stay None on artifacts.

    Schema (all keys optional):

        areas:
          payments:
            paths: ["src/payments/**", "src/billing/**"]
          auth:
            paths: ["src/auth/**"]

        inline_comment_authors:
          - sara
          - marcus

    The YAML parser is the stdlib-free 'simple subset' below — we deliberately
    don't add a PyYAML dep just for one config file. If the user writes valid
    YAML in the documented shape, it parses; weirder YAML falls back to {}.
    """
    p = contexts_path(repo_root)
    if not p.exists():
        return {}, set()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}, set()
    return _parse_contexts_yaml(text)


# ── Minimal YAML subset parser ────────────────────────────────────────────────
# Supports exactly the documented contexts.yaml shape: top-level mapping of
# `areas:` (mapping of name → mapping with `paths:` list) and
# `inline_comment_authors:` (list of strings). No anchors, no flow style,
# no multi-line strings. Anything outside the schema is silently ignored.

def _parse_contexts_yaml(text: str) -> tuple[dict[str, list[str]], set[str]]:
    areas: dict[str, list[str]] = {}
    inline: set[str] = set()

    section: str | None = None     # "areas" | "inline" | None
    current_area: str | None = None
    in_paths: bool = False

    for raw_line in text.splitlines():
        # Strip comments (after #) and trailing whitespace.
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.lstrip(" ")

        if indent == 0:
            current_area = None
            in_paths = False
            if stripped.rstrip(":") == "areas" and stripped.endswith(":"):
                section = "areas"
            elif stripped.rstrip(":") == "inline_comment_authors" and stripped.endswith(":"):
                section = "inline"
            else:
                section = None
            continue

        if section == "areas":
            if indent == 2 and stripped.endswith(":"):
                current_area = stripped[:-1].strip()
                areas.setdefault(current_area, [])
                in_paths = False
            elif indent == 4 and stripped.startswith("paths:"):
                rest = stripped[len("paths:"):].strip()
                in_paths = True
                if rest.startswith("[") and rest.endswith("]"):
                    # Inline list form: paths: ["a", "b"]
                    inner = rest[1:-1]
                    items = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]
                    if current_area is not None:
                        areas[current_area].extend(items)
                    in_paths = False
            elif in_paths and stripped.startswith("- "):
                item = stripped[2:].strip().strip('"').strip("'")
                if item and current_area is not None:
                    areas[current_area].append(item)
        elif section == "inline":
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip('"').strip("'")
                if item:
                    inline.add(item)

    return areas, inline


# ── Area resolution ───────────────────────────────────────────────────────────

def areas_for_paths(
    paths: Iterable[str],
    areas: dict[str, list[str]],
) -> set[str]:
    """Return area names whose globs match any of the given repo-relative paths.

    Uses fnmatch-style glob matching with `**` semantics: a glob like
    ``src/payments/**`` matches any path under ``src/payments/``. A path
    matching no area returns an empty set; the caller decides whether to bucket
    such artifacts as "uncategorized" or drop them.
    """
    import fnmatch

    matched: set[str] = set()
    norm_paths = [str(p).replace("\\", "/") for p in paths]
    for area, globs in areas.items():
        for g in globs:
            # Normalise ** to * for fnmatch (which doesn't distinguish).
            pat = g.replace("**", "*")
            for p in norm_paths:
                if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat.rstrip("/*") + "/*"):
                    matched.add(area)
                    break
            if area in matched:
                break
    return matched


# ── Public re-exports ─────────────────────────────────────────────────────────

# replace() is convenient for tests building modified Artifacts; re-export so
# tests don't reach into dataclasses.
__all__ = [
    "wiki_root", "raw_root", "raw_dir", "raw_cache_dir", "raw_notes_dir",
    "notes_dir", "curated_dir", "skills_dir", "metrics_dir",
    "gatherers_dir", "contexts_path",
    "cursor_path", "sha_cache_path", "dirty_path", "gather_log_path",
    "ensure_layout",
    "artifact_path", "write_artifact", "read_artifact", "list_artifacts",
    "write_note_raw", "write_session_insight",
    "read_cursor", "write_cursor",
    "sha_seen", "sha_record",
    "read_dirty", "mark_dirty", "clear_dirty",
    "log_error",
    "load_contexts", "areas_for_paths",
    "replace",
]
