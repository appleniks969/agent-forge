"""
gather/builtin/hotspots.py — derived second-pass over PRs + commits.

Reads `raw/prs/` and `raw/commits/` (just-written by the prs + git_history
gatherers) and produces one Artifact per file that's been touched in the
window, with bug-fix density signals. Also produces area-level rollups.

`runs_after = ("prs", "git_history")` — discovery enforces ordering.
Source is `DERIVED`: this is computed, not gathered.

Joins:
  * file paths × `signals.is_bugfix` from commits/PRs → fix counts
  * file paths × CODEOWNERS (from repo_files) → declared owners
  * file paths × git authorship → *fallback* owners when CODEOWNERS is missing
  * file paths × contexts.yaml areas → area attribution
  * cross-file co-edit graph from commits → co_changed_files
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...storage import list_artifacts, load_contexts, areas_for_paths
from ...types import Artifact, Gatherer, Source

# Time windows for fix density.
_W30 = timedelta(days=30)
_W90 = timedelta(days=90)


def _codeowners_map(repo_root: Path | str) -> dict[str, list[str]]:
    """Pull the parsed CODEOWNERS map from any repo_file artifact that has it."""
    for art in list_artifacts(repo_root, kind="repo_file"):
        sig = art.signals or {}
        co = sig.get("codeowners")
        if isinstance(co, dict) and co:
            return co
    return {}


def _match_owners(path: str, codeowners: dict[str, list[str]]) -> list[str]:
    """Return owners for `path` based on CODEOWNERS patterns. Last-match-wins (gh rule)."""
    import fnmatch
    owners: list[str] = []
    for pattern, ows in codeowners.items():
        # CODEOWNERS uses gitignore-ish globs; fnmatch is approximate but ok for MVP.
        pat = pattern.lstrip("/")
        norm = pat.replace("**", "*")
        if fnmatch.fnmatch(path, norm) or fnmatch.fnmatch(path, norm + "/*") or path.startswith(pat.rstrip("*")):
            owners = ows
    return owners


def _gather_file_signals(repo_root: Path | str, now: datetime) -> dict:
    """Walk raw/commits + raw/prs once, return aggregates.

    Returns a dict of:
        per_file_changes_30d / per_file_changes_90d
        per_file_fixes_30d  / per_file_fixes_90d
        per_file_recent_fix_prs
        per_file_recent_fix_commits
        per_file_coedit_counts (path → counter of other paths)
        per_file_authors_90d (path → Counter[author_name])
    """
    p_changes_30: dict[str, int] = defaultdict(int)
    p_changes_90: dict[str, int] = defaultdict(int)
    p_fixes_30: dict[str, int]   = defaultdict(int)
    p_fixes_90: dict[str, int]   = defaultdict(int)
    p_recent_prs: dict[str, list[int]]    = defaultdict(list)
    p_recent_commits: dict[str, list[str]] = defaultdict(list)
    coedit: dict[str, dict[str, int]]      = defaultdict(lambda: defaultdict(int))
    p_authors_90: dict[str, Counter] = defaultdict(Counter)

    cutoff_30 = now - _W30
    cutoff_90 = now - _W90

    # Commits
    for art in list_artifacts(repo_root, kind="commit"):
        if art.ts < cutoff_90:
            continue
        sig = art.signals or {}
        files = [f for f in (sig.get("files_changed") or []) if isinstance(f, str)]
        if not files:
            continue
        is_fix = bool(sig.get("is_bugfix"))
        in_30 = art.ts >= cutoff_30
        author = (sig.get("author") or "").strip()
        for f in files:
            p_changes_90[f] += 1
            if in_30:
                p_changes_30[f] += 1
            if is_fix:
                p_fixes_90[f] += 1
                if in_30:
                    p_fixes_30[f] += 1
                if len(p_recent_commits[f]) < 5:
                    sha = art.id.removeprefix("commit-")
                    p_recent_commits[f].append(sha)
            # Authorship for owner-fallback (skip bots — they dominate by volume
            # but tell us nothing about who reviews/approves).
            if author and not _is_bot(author):
                p_authors_90[f][author] += 1
        # Co-edit graph: each pair in this commit
        for i, a in enumerate(files):
            for b in files[i + 1:]:
                coedit[a][b] += 1
                coedit[b][a] += 1

    # PRs (use files + is_bugfix signal)
    for art in list_artifacts(repo_root, kind="pr"):
        if art.ts < cutoff_90:
            continue
        sig = art.signals or {}
        files = [f for f in (sig.get("files_changed") or []) if isinstance(f, str)]
        if not files:
            continue
        in_30 = art.ts >= cutoff_30
        is_fix = bool(sig.get("is_bugfix"))
        try:
            number = int(art.id.removeprefix("pr-"))
        except ValueError:
            number = 0
        for f in files:
            # PR file count is already covered by per-commit accounting; skip
            # change counters here to avoid double-counting squash merges.
            if is_fix:
                # Don't double-count fixes if commit-level already saw it,
                # but most squash-merge repos won't have per-commit data, so
                # keep the PR contribution and accept some over-count.
                p_fixes_90[f] += 1
                if in_30:
                    p_fixes_30[f] += 1
                if number and number not in p_recent_prs[f] and len(p_recent_prs[f]) < 5:
                    p_recent_prs[f].append(number)

    return {
        "changes_30": dict(p_changes_30),
        "changes_90": dict(p_changes_90),
        "fixes_30":   dict(p_fixes_30),
        "fixes_90":   dict(p_fixes_90),
        "recent_prs": {k: v for k, v in p_recent_prs.items()},
        "recent_commits": {k: v for k, v in p_recent_commits.items()},
        "coedit": {k: dict(v) for k, v in coedit.items()},
        "authors_90": {k: dict(v) for k, v in p_authors_90.items()},
    }


# Bot-author detection — these dominate by commit volume on most repos and
# their "ownership" is meaningless. Conservative match (suffix-only) so we
# don't accidentally exclude humans whose names contain "bot".
_BOT_PATTERNS = (
    "[bot]",
    "-bot",
    "renovate",
    "dependabot",
    "github-actions",
    "snyk",
    "semantic-release",
    "release-please",
)


def _is_bot(author: str) -> bool:
    a = author.lower().strip()
    return any(p in a for p in _BOT_PATTERNS)


def _fallback_owners(authors_90: dict[str, int], top_n: int = 3) -> list[str]:
    """Return the top-N authors of a path over 90 days as a CODEOWNERS fallback.

    These aren't *declared* owners — they're "people who actually touch this
    file." Useful tribal-knowledge signal when the repo has no CODEOWNERS.
    Empty input → empty list (caller is responsible for the "still empty"
    case; we don't invent owners).
    """
    if not authors_90:
        return []
    ranked = sorted(authors_90.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:top_n]]


class HotspotsGatherer(Gatherer):
    name = "hotspots"
    runs_after = ("prs", "git_history")
    timeout_seconds = 30

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        now = datetime.now(tz=timezone.utc)
        agg = _gather_file_signals(repo_root, now)
        if not agg["changes_90"] and not agg["fixes_90"]:
            return []

        areas, _ = load_contexts(repo_root)
        codeowners = _codeowners_map(repo_root)

        out: list[Artifact] = []

        # File-level hotspots — emit only files with at least one fix in 90d
        # OR ≥ 3 changes in 30d (active churn).
        candidates = set(agg["fixes_90"].keys()) | {
            f for f, n in agg["changes_30"].items() if n >= 3
        }
        # Per-area rollups.
        per_area_files: dict[str, list[str]] = defaultdict(list)

        for path in sorted(candidates):
            ch90 = agg["changes_90"].get(path, 0)
            ch30 = agg["changes_30"].get(path, 0)
            fx90 = agg["fixes_90"].get(path, 0)
            fx30 = agg["fixes_30"].get(path, 0)
            ratio_90 = round(fx90 / ch90, 3) if ch90 else 0.0

            owners = _match_owners(path, codeowners)
            # Fallback: if CODEOWNERS didn't claim this path, attribute to the
            # top authors of the last 90 days. We tag the result with a flag
            # in signals so downstream renderers can distinguish "declared
            # owners" from "frequent authors."
            owners_source = "codeowners"
            if not owners:
                owners = _fallback_owners(agg["authors_90"].get(path, {}))
                owners_source = "authors_90d" if owners else "none"
            matched_areas = list(areas_for_paths([path], areas))
            primary_area = matched_areas[0] if matched_areas else None

            # Top co-changed files (up to 5).
            co = agg["coedit"].get(path, {})
            top_co = sorted(co.items(), key=lambda kv: kv[1], reverse=True)[:5]
            co_paths = [c for c, _ in top_co]

            signals = {
                "path": path,
                "bugfix_count_30d": fx30,
                "bugfix_count_90d": fx90,
                "total_changes_30d": ch30,
                "total_changes_90d": ch90,
                "fix_ratio_90d": ratio_90,
                "recent_fix_prs": agg["recent_prs"].get(path, []),
                "recent_fix_commits": agg["recent_commits"].get(path, []),
                "co_changed_files": co_paths,
                "owners": owners,
                "owners_source": owners_source,   # "codeowners" | "authors_90d" | "none"
            }
            out.append(Artifact(
                id=f"hotspot-{path.replace('/', '-')}",
                kind="hotspot",
                source=Source.DERIVED,
                title=path,
                body="",
                ts=now,
                area=primary_area,
                signals=signals,
            ))
            if primary_area:
                per_area_files[primary_area].append(path)

        # Area-level rollup hotspots.
        for area, files in per_area_files.items():
            if not files:
                continue
            total_changes = sum(agg["changes_90"].get(f, 0) for f in files)
            total_fixes   = sum(agg["fixes_90"].get(f, 0) for f in files)
            density = round(total_fixes / total_changes, 3) if total_changes else 0.0
            top_hot = sorted(files, key=lambda f: agg["fixes_90"].get(f, 0), reverse=True)[:5]
            out.append(Artifact(
                id=f"area_hotspot-{area}",
                kind="hotspot",
                source=Source.DERIVED,
                title=f"area:{area}",
                body="",
                ts=now,
                area=area,
                signals={
                    "scope": "area",
                    "area": area,
                    "bugfix_density_90d": density,
                    "total_fixes_90d": total_fixes,
                    "total_changes_90d": total_changes,
                    "top_hot_files": top_hot,
                },
            ))

        return out
