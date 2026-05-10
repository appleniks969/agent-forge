"""
gather/builtin/code_markers.py — pull TODO/FIXME/HACK and lint-suppression markers.

Greps the source tree for self-documenting tech-debt markers. Each unique
file with at least one marker becomes one Artifact (kind="code_marker") whose
body is the list of marker lines. Compile (MVP 3) reads these to surface
"hot spots in code" — places the team has explicitly flagged.

Uses ripgrep when available (10-100x faster on large trees) and falls back
to a Python walker when not. Both go through `_subprocess.run` for the rg
case so abort signals propagate.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent_forge._subprocess import run as run_subprocess

from ...types import Artifact, Gatherer, Source

# Patterns we look for. The keyword family ``TODO|FIXME|XXX|HACK|NOTE`` is
# matched by a *negative* trailing-character class instead of the original
# ``[: ]``. Original required a colon-or-space immediately after the keyword,
# which silently dropped the very common ``// TODO(scope): …`` and
# ``// FIXME[area]: …`` styles because ``(`` / ``[`` ended the ``\b…\b``
# boundary on a non-word char *that wasn't whitespace or a colon*. The new
# rule matches the keyword as a whole word followed by anything that isn't a
# letter/digit/underscore — i.e. a real word boundary — which catches every
# common convention while still rejecting "TODOlist" and "NOTEPAD".
_MARKER_KEYWORDS = ("TODO", "FIXME", "XXX", "HACK", "NOTE")
_MARKER_KW_RE = r"\b(?:" + "|".join(_MARKER_KEYWORDS) + r")\b(?=[^A-Za-z0-9_]|$)"
_MARKER_RE = re.compile(
    _MARKER_KW_RE
    + r"|\bnoqa\b"
    + r"|\beslint-disable\b"
    + r"|#\s*type:\s*ignore\b"
    + r"|@SuppressWarnings\b"
    + r"|@deprecated\b"
)

# Skip these directories — never useful to scan.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
              ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build",
              "target", ".idea", ".vscode", ".agent-forge"}

_SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
                ".kt", ".kts", ".rb", ".swift", ".c", ".cpp", ".h", ".hpp",
                ".cs", ".php", ".scala", ".sh", ".bash", ".zsh"}

_MAX_FILES = 5_000          # safety cap
_MAX_MATCHES_PER_FILE = 50  # don't blow up on auto-generated noise


async def _rg_scan(repo_root: Path) -> dict[Path, list[tuple[int, str]]]:
    """Run ripgrep, parse output. Returns {path: [(lineno, line), ...]}."""
    # Use Rust regex flavour (default in rg). Lookaheads aren't supported,
    # so we approximate "keyword + non-word-char" with a character class.
    # `[^\w]` would also exclude end-of-line; rg matches per-line so a TODO
    # at literal EOL needs an alternation. Easiest: the bare word boundary
    # plus a separate negative-pattern in Python-side filtering would be
    # over-engineering — accept that ``\bTODO\b`` already excludes ``TODOlist``,
    # and tolerate the rare false positive on ``# NOTEPAD``.
    kw_alt = "|".join(_MARKER_KEYWORDS)
    cmd = [
        "rg", "--no-heading", "--with-filename", "--line-number",
        "-e", rf"\b({kw_alt})\b",
        "-e", r"\bnoqa\b",
        "-e", r"\beslint-disable\b",
        "-e", r"#\s*type:\s*ignore\b",
        "-e", r"@SuppressWarnings\b",
        "-e", r"@deprecated\b",
    ]
    for d in _SKIP_DIRS:
        cmd.extend(["-g", f"!**/{d}/**"])
    res = await run_subprocess(cmd, cwd=str(repo_root), timeout=30.0)
    if res.aborted or res.returncode not in (0, 1):  # 1 = no matches
        return {}
    out: dict[Path, list[tuple[int, str]]] = {}
    for raw in res.stdout.splitlines():
        # Format: <path>:<lineno>:<text>
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        path_s, ln_s, text = parts
        try:
            ln = int(ln_s)
        except ValueError:
            continue
        p = repo_root / path_s
        out.setdefault(p, []).append((ln, text.rstrip()))
    return out


def _python_scan(repo_root: Path) -> dict[Path, list[tuple[int, str]]]:
    """Fallback walker when rg isn't installed. Slower, but works everywhere."""
    out: dict[Path, list[tuple[int, str]]] = {}
    count = 0
    for p in repo_root.rglob("*"):
        if count >= _MAX_FILES:
            break
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _SOURCE_EXTS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches: list[tuple[int, str]] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if _MARKER_RE.search(line):
                matches.append((i, line.strip()))
                if len(matches) >= _MAX_MATCHES_PER_FILE:
                    break
        if matches:
            out[p] = matches
        count += 1
    return out


class CodeMarkersGatherer(Gatherer):
    name = "code_markers"
    timeout_seconds = 45

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        if not repo_root.exists():
            return []
        if shutil.which("rg"):
            scanned = await _rg_scan(repo_root)
        else:
            scanned = _python_scan(repo_root)

        now = datetime.now(tz=timezone.utc)
        out: list[Artifact] = []
        for path, matches in scanned.items():
            try:
                rel = str(path.relative_to(repo_root))
            except ValueError:
                continue
            body_lines = [f"{ln}: {text}" for ln, text in matches[:_MAX_MATCHES_PER_FILE]]
            body = "\n".join(body_lines)
            out.append(Artifact(
                id=f"markers-{rel.replace('/', '-')}",
                kind="code_marker",
                source=Source.BUILTIN,
                title=rel,
                body=body,
                ts=now,
                signals={
                    "path": rel,
                    "marker_count": len(matches),
                    "kinds": sorted({m[1].split()[0] for m in matches if m[1].split()})[:10],
                },
            ))
        return out
