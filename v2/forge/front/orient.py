"""Orientation readers: file-reading suppliers for the prompt's session sections.

Layer: front — imports everything; nothing imports it. These are the SUPPLIERS
that policy's SectionThunks call. Policy stays pure: it never reads a file or
runs git; it asks a supplier for text. The composition root wires these into
the corresponding `*_section()` thunks (policy/prompt.py).

Each public factory returns a SYNC closure `() -> str | None` that is CHEAP per
call — it is invoked on EVERY prompt build. Cheapness comes from caching: a
supplier re-does its expensive work (read, git walk) only when a stat-level
trigger changes (the chosen file's mtime, or .git/HEAD + .git/index mtime, or
the top-level directory's mtime). Returning None (or whitespace) omits the
section from that build, so an absent AGENTS.md or empty memory simply vanishes.

Why suppliers and not snapshots: editing AGENTS.md or running /remember
mid-session refreshes the prompt on the next build — a capability v1 lacked,
because v1 captured the file contents once at session start.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

# Caps / budgets (bytes). Documented constants so the prompt's section sizes are
# auditable in one place.
_AGENTS_CAP = 32 * 1024
_REPO_MAP_BUDGET = 6 * 1024
_SKILLS_BUDGET = 4 * 1024
_MEMORY_CAP = 8 * 1024

# Candidate project-instruction files, in precedence order (first found wins).
_AGENTS_CANDIDATES = ("AGENTS.md", "CLAUDE.md", ".agent-forge/instructions.md")

# Directories never worth showing in a non-git repo map.
_REPO_MAP_IGNORE = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        "dist",
        "build",
        ".next",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        ".idea",
        ".tox",
    }
)
# Hard ceiling on the rglob fallback so an enormous non-git tree can never make
# a "cheap" supplier expensive on the first call.
_RGLOB_SCAN_CAP = 5000

# git subprocess guardrails: bounded time, recency window, and how many recent
# commits to inspect for the change-weighting. None of these run per build —
# only when the cache is cold or invalidated.
_GIT_TIMEOUT = 5.0
_GIT_LOG_COMMITS = 60


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# --- project instructions (AGENTS.md / CLAUDE.md / instructions.md) -------------


def agents_doc_supplier(cwd: Path) -> Callable[[], str | None]:
    """Supply the project-instructions section.

    Reads the first existing of AGENTS.md, CLAUDE.md, .agent-forge/instructions.md
    under `cwd`, capped at 32KB with a truncation note, under a
    '# Project instructions' header. mtime-cached on the CHOSEN file's path +
    mtime: editing the file mid-session refreshes on the next build; switching
    which candidate exists is picked up because the cache key includes the path.
    """
    cwd = Path(cwd)
    # cache: (chosen_path, mtime) -> rendered text. A miss recomputes.
    cache: dict[str, object] = {"key": None, "text": None}

    def resolve() -> str | None:
        chosen: Path | None = None
        mtime: float | None = None
        for name in _AGENTS_CANDIDATES:
            p = cwd / name
            m = _mtime_or_none(p)
            if m is not None:
                chosen, mtime = p, m
                break
        if chosen is None:
            cache["key"] = None
            cache["text"] = None
            return None
        key = (str(chosen), mtime)
        if cache["key"] == key:
            return cache["text"]  # type: ignore[return-value]
        try:
            text = chosen.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(text) > _AGENTS_CAP:
            text = text[:_AGENTS_CAP] + "\n\n[truncated — file exceeds 32KB]"
        rendered = "# Project instructions\n" + text.strip()
        cache["key"] = key
        cache["text"] = rendered
        return rendered

    return resolve


# --- repo map (git-recency-weighted, byte-budgeted) -----------------------------


def repo_map_supplier(cwd: Path) -> Callable[[], str | None]:
    """Supply a git-recency-weighted repository map for large repos.

    Strategy: list tracked files via `git -C cwd ls-files`; order directories by
    how recently their files changed (from `git log --name-only`), always show
    the top two directory levels, then expand the most-recently-touched areas
    until a HARD ~6KB byte budget, ending with an 'N more files' overflow line.
    Non-git repos fall back to a bounded rglob over `cwd` with an ignore set.

    COST RULE: the expensive walk runs AT MOST ONCE per session. The result is
    cached and only recomputed when the invalidation trigger changes:
      - git repo:  .git/HEAD mtime OR .git/index mtime (a commit, checkout,
                   or `git add` moves one of these; nothing else need recompute).
      - non-git:   the top-level directory's own mtime (new/removed top entries).
    A plain prompt build between commits is a single stat pair, no git call.
    """
    cwd = Path(cwd)
    git_dir = _git_dir(cwd)
    cache: dict[str, object] = {"key": None, "text": None}

    def resolve() -> str | None:
        if git_dir is not None:
            key: object = (
                "git",
                _mtime_or_none(git_dir / "HEAD"),
                _mtime_or_none(git_dir / "index"),
            )
        else:
            key = ("nogit", _mtime_or_none(cwd))
        if cache["key"] == key and cache["text"] is not None:
            return cache["text"]  # type: ignore[return-value]
        try:
            files = (
                _git_tracked_ordered(cwd)
                if git_dir is not None
                else _rglob_files(cwd)
            )
            text = _render_repo_map(files) if files else None
        except Exception:
            text = None
        cache["key"] = key
        cache["text"] = text
        return text

    return resolve


def _git_dir(cwd: Path) -> Path | None:
    """Absolute .git directory for `cwd`, or None if cwd is not a git work tree.

    Uses `git rev-parse --absolute-git-dir` so this is correct from a subdir,
    inside a linked worktree, or when .git is a gitdir-pointer file."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    return Path(path) if path else None


def _git_tracked_ordered(cwd: Path) -> list[str]:
    """Tracked files (relative to cwd's prefix) ordered by recent change.

    Files touched in recent commits come first (in commit-recency order);
    everything else follows in path order. `git ls-files` and `git log` are both
    scoped to `cwd` so a map built from a subdir shows that subtree only."""
    tracked = _git_lines(cwd, ["ls-files"])
    if not tracked:
        return []
    tracked_set = set(tracked)
    recent: list[str] = []
    seen: set[str] = set()
    for path in _git_lines(
        cwd,
        ["log", f"-n{_GIT_LOG_COMMITS}", "--name-only", "--pretty=format:"],
    ):
        if path in tracked_set and path not in seen:
            recent.append(path)
            seen.add(path)
    rest = sorted(p for p in tracked if p not in seen)
    return recent + rest


def _git_lines(cwd: Path, args: list[str]) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _rglob_files(cwd: Path) -> list[str]:
    """Bounded, ignore-filtered walk for non-git trees. Path-sorted (no git
    recency available); hard-capped so it stays cheap on the first call."""
    files: list[str] = []
    try:
        for p in sorted(cwd.rglob("*")):
            if any(part in _REPO_MAP_IGNORE for part in p.relative_to(cwd).parts):
                continue
            if p.is_file():
                files.append(str(p.relative_to(cwd)))
                if len(files) >= _RGLOB_SCAN_CAP:
                    break
    except OSError:
        return files
    return files


def _render_repo_map(files: Sequence[str]) -> str:
    """Render the recency-ordered file list under a byte budget.

    Always emits the top-2 directory levels (the shape of the repo), then keeps
    appending recency-ordered files until ~6KB, then an 'N more files' line. The
    input order IS the priority order, so truncation drops the least-recently
    touched files."""
    header = "# Repository map\n"
    top_levels = _top_level_overview(files)
    budget = _REPO_MAP_BUDGET - len(header) - len(top_levels)

    shown: list[str] = []
    used = 0
    for i, path in enumerate(files):
        line = path + "\n"
        # Always reserve room for an overflow line if files remain.
        remaining = len(files) - i
        overflow_cost = len(f"… {remaining} more files\n") if remaining > 1 else 0
        if used + len(line) + overflow_cost > budget and shown:
            break
        shown.append(path)
        used += len(line)

    body_lines = list(shown)
    omitted = len(files) - len(shown)
    if omitted > 0:
        body_lines.append(f"… {omitted} more files")
    return header + top_levels + "\n".join(body_lines)


def _top_level_overview(files: Sequence[str]) -> str:
    """Two-level directory shape: each top dir (and its immediate subdir) with a
    file count.

    Cheap orientation that survives truncation — the reader always sees the
    repo's skeleton even when the file list below is budget-clipped. Files at the
    repo root are bucketed under '(root)'."""
    counts: dict[str, int] = {}
    for path in files:
        parts = path.split("/")
        if len(parts) == 1:
            bucket = "(root)"
        elif len(parts) == 2:
            bucket = parts[0] + "/"
        else:
            bucket = parts[0] + "/" + parts[1] + "/"
        counts[bucket] = counts.get(bucket, 0) + 1
    if not counts:
        return ""
    lines = [f"{name} ({n})" for name, n in sorted(counts.items())]
    return "directories: " + ", ".join(lines) + "\n"


# --- memory (merged global + project memory.md) ---------------------------------


def memory_supplier(cwd: Path) -> Callable[[], str | None]:
    """Supply the merged global + project memory section.

    Global memory lives at ~/.agent-forge/memory.md, project memory at
    <cwd>/.agent-forge/memory.md (ported from v1 session.memory_path). The two
    are concatenated under a '# Memory' header and capped at ~8KB. mtime-cached
    on BOTH files' mtimes, so /remember (which rewrites project memory) refreshes
    the next build. Returns None when both are absent or empty."""
    cwd = Path(cwd)
    global_path = Path.home() / ".agent-forge" / "memory.md"
    project_path = cwd / ".agent-forge" / "memory.md"
    cache: dict[str, object] = {"key": None, "text": None}

    def resolve() -> str | None:
        key = (_mtime_or_none(global_path), _mtime_or_none(project_path))
        if cache["key"] == key:
            return cache["text"]  # type: ignore[return-value]
        parts: list[str] = []
        for p in (global_path, project_path):
            try:
                chunk = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if chunk:
                parts.append(chunk)
        merged = "\n".join(parts).strip()
        if not merged:
            text: str | None = None
        else:
            if len(merged) > _MEMORY_CAP:
                merged = merged[:_MEMORY_CAP] + "\n\n[truncated — memory exceeds 8KB]"
            text = "# Memory\n" + merged
        cache["key"] = key
        cache["text"] = text
        return text

    return resolve


# --- skills index (rendered from adapters.skills.discover_skills) ---------------


def skills_index_supplier(roots: Sequence[Path]) -> Callable[[], str | None]:
    """Supply the skills catalogue: one '<name> — <description>' line per skill.

    Delegates discovery to forge.adapters.skills.discover_skills(roots), which is
    itself stat-cached, so this stays cheap per build. Output is byte-budgeted
    (~4KB) with an overflow note. Returns None when no skills resolve. The import
    is deferred to call time so this module loads even if the skills adapter is
    not yet on disk; an import/lookup failure degrades to no section."""
    roots = tuple(Path(r) for r in roots)

    def resolve() -> str | None:
        try:
            from forge.adapters.skills import discover_skills
        except Exception:
            return None
        try:
            metas = discover_skills(roots)
        except Exception:
            return None
        if not metas:
            return None
        header = "Available skills (invoke with the Skill tool):\n"
        budget = _SKILLS_BUDGET - len(header)
        shown: list[str] = []
        used = 0
        for i, meta in enumerate(metas):
            desc = " ".join((meta.description or "").split())
            line = f"{meta.name} — {desc}" if desc else meta.name
            entry = line + "\n"
            remaining = len(metas) - i
            overflow_cost = (
                len(f"… {remaining} more skills\n") if remaining > 1 else 0
            )
            if used + len(entry) + overflow_cost > budget and shown:
                break
            shown.append(line)
            used += len(entry)
        omitted = len(metas) - len(shown)
        if omitted > 0:
            shown.append(f"… {omitted} more skills")
        return header + "\n".join(shown)

    return resolve
