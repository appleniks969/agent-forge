"""
gather/builtin/repo_files.py — pull known doc/config files from the repo tree.

Walks a curated allowlist of paths (README, CHANGELOG, CONTRIBUTING,
ARCHITECTURE, SECURITY, CODEOWNERS, AGENTS.md, etc.) plus directories that
conventionally contain decision/incident docs (docs/adr/, docs/decisions/,
docs/incidents/, runbooks/, etc.).

Each file becomes one Artifact with `kind` set to the document type
(`adr`, `incident`, `repo_file`). CODEOWNERS gets parsed into a per-path
ownership map stored in signals — hotspots.py joins against this.

Local + free + universal. No subprocess, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...types import Artifact, Gatherer, Source

# ── Allowlists ────────────────────────────────────────────────────────────────

# Top-level files we always pull when present. Case-insensitive match.
_ROOT_FILES: tuple[tuple[str, str], ...] = (
    # (filename pattern lowercased, kind)
    ("readme.md",          "repo_file"),
    ("readme.rst",         "repo_file"),
    ("readme",             "repo_file"),
    ("changelog.md",       "repo_file"),
    ("changelog",          "repo_file"),
    ("history.md",         "repo_file"),
    ("releases.md",        "repo_file"),
    ("contributing.md",    "repo_file"),
    ("architecture.md",    "repo_file"),
    ("security.md",        "repo_file"),
    ("codeowners",         "repo_file"),   # also matched in .github/ below
    ("agents.md",          "repo_file"),
    ("claude.md",          "repo_file"),
    ("license",            "repo_file"),
    ("license.md",         "repo_file"),
)

# Other well-known locations.
_EXTRA_FILES: tuple[str, ...] = (
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
)

# Directories that conventionally contain ADR or incident docs. (dir, kind).
_DOC_DIRS: tuple[tuple[str, str], ...] = (
    ("docs/adr",                "adr"),
    ("docs/adrs",               "adr"),
    ("docs/decisions",          "adr"),
    ("doc/adr",                 "adr"),
    ("architecture/decisions",  "adr"),
    ("docs/incidents",          "incident"),
    ("docs/postmortems",        "incident"),
    ("postmortems",             "incident"),
    ("runbooks",                "incident"),
    ("docs/runbooks",           "incident"),
    ("docs/rfcs",               "adr"),    # treat RFCs as ADR-class
    ("rfcs",                    "adr"),
)

_MAX_BODY_BYTES = 200_000  # 200 KB cap per artifact body — README rarely larger


def _read_capped(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > _MAX_BODY_BYTES:
        return text[:_MAX_BODY_BYTES] + "\n\n[... truncated by repo_files gatherer ...]"
    return text


def _mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(tz=timezone.utc)


def _parse_codeowners(text: str) -> dict[str, list[str]]:
    """Parse CODEOWNERS into {path_pattern: [owners]}.

    Format: each non-comment, non-blank line is `<pattern> @owner1 @owner2 ...`.
    Returns the raw map; hotspots.py does the path matching.
    """
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, *owners = parts
        out[pattern] = [o for o in owners if o.startswith("@") or "@" in o]
    return out


class RepoFilesGatherer(Gatherer):
    name = "repo_files"

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        out: list[Artifact] = []

        # 1. Root files (case-insensitive lookup).
        if repo_root.exists():
            existing = {p.name.lower(): p for p in repo_root.iterdir() if p.is_file()}
            for name_lc, kind in _ROOT_FILES:
                p = existing.get(name_lc)
                if p is None:
                    continue
                body = _read_capped(p)
                if body is None:
                    continue
                rel = str(p.relative_to(repo_root))
                signals: dict = {"path": rel}
                if name_lc.startswith("codeowners"):
                    signals["codeowners"] = _parse_codeowners(body)
                out.append(Artifact(
                    id=f"repo_file-{rel.replace('/', '-')}",
                    kind=kind,
                    source=Source.BUILTIN,
                    title=p.name,
                    body=body,
                    ts=_mtime(p),
                    signals=signals,
                ))

        # 2. Extra well-known paths.
        for rel in _EXTRA_FILES:
            p = repo_root / rel
            if not p.is_file():
                continue
            body = _read_capped(p)
            if body is None:
                continue
            signals: dict = {"path": rel}
            if "CODEOWNERS" in rel:
                signals["codeowners"] = _parse_codeowners(body)
            out.append(Artifact(
                id=f"repo_file-{rel.replace('/', '-')}",
                kind="repo_file",
                source=Source.BUILTIN,
                title=Path(rel).name,
                body=body,
                ts=_mtime(p),
                signals=signals,
            ))

        # 3. Documented directories.
        for rel_dir, kind in _DOC_DIRS:
            d = repo_root / rel_dir
            if not d.is_dir():
                continue
            for path in sorted(d.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in (".md", ".markdown", ".rst", ".txt"):
                    continue
                body = _read_capped(path)
                if body is None:
                    continue
                rel = str(path.relative_to(repo_root))
                out.append(Artifact(
                    id=f"{kind}-{rel.replace('/', '-')}",
                    kind=kind,
                    source=Source.BUILTIN,
                    title=path.stem,
                    body=body,
                    ts=_mtime(path),
                    signals={"path": rel},
                ))

        return out
