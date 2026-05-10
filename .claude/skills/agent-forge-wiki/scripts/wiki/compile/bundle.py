"""
compile/bundle.py — render raw/ artifacts into a JSON bundle for the LLM.

Compile sees a *summary* of raw/, never the raw data itself. We pre-rank
and pre-truncate so the LLM gets a useful, bounded view:

  - hotspots: top 20 files by 30-day commit count + bugfix count
  - recent_commits: last 30 commits with title + bugfix flag + areas
  - prs: last 20 merged PRs with title + body excerpt + areas
  - notes: every note (always — they're hand-curated and small)
  - session_insights: every raw/notes/session/*.md (small, all included)
  - adrs: every adr artifact (small)
  - markers: top 30 code_markers by recency

The result is fed into compile.runner as the {raw_bundle} block.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .._noise import is_noisy_path
from ..storage import list_artifacts, raw_notes_dir
from ..types import Artifact


def build_compile_bundle(
    repo_root: Path | str,
    *,
    area: str | None = None,
    max_items_per_section: int = 30,
) -> str:
    """Return a JSON-formatted bundle of raw/ artifacts the LLM should compile.

    If ``area`` is set, filter every section to artifacts attributed to that
    area (or whose ``files_changed`` resolves to it via signals). Used by
    per-area page generation.
    """
    root = Path(repo_root)

    def _f(arts: list[Artifact]) -> list[Artifact]:
        if not area:
            return arts
        return [a for a in arts if _matches_area(a, area)]

    # Hotspots: drop noise (CHANGELOG, lockfiles, *.generated.*, .github/, …)
    # before ranking so they never reach the LLM. Then sort by 30-day total
    # changes + bugfix count. The legacy `commits_30d` field name was a bug —
    # the gatherer writes `total_changes_30d`.
    hotspots = _f([
        h for h in list(list_artifacts(root, kind="hotspot"))
        if not is_noisy_path((h.signals or {}).get("path", ""))
    ])
    hotspots = sorted(
        hotspots,
        key=lambda a: (
            -int((a.signals or {}).get("total_changes_30d", 0) or 0),
            -int((a.signals or {}).get("bugfix_count_30d", 0) or 0),
        ),
    )[:20]

    commits = _f(sorted(
        list(list_artifacts(root, kind="commit")),
        key=lambda a: a.ts, reverse=True,
    ))[:max_items_per_section]

    prs = _f(sorted(
        list(list_artifacts(root, kind="pr")),
        key=lambda a: a.ts, reverse=True,
    ))[:20]

    adrs = _f(list(list_artifacts(root, kind="adr")))

    notes = _f(sorted(
        list(list_artifacts(root, kind="note")),
        key=lambda a: a.ts, reverse=True,
    ))

    markers = _f(sorted(
        list(list_artifacts(root, kind="code_marker")),
        key=lambda a: a.ts, reverse=True,
    ))[:max_items_per_section]

    session_insights = _read_session_insights(root)

    bundle = {
        "area": area,
        "counts": {
            "hotspots": len(hotspots),
            "commits": len(commits),
            "prs": len(prs),
            "adrs": len(adrs),
            "notes": len(notes),
            "markers": len(markers),
            "session_insights": len(session_insights),
        },
        "hotspots": [_artifact_summary(a, body_chars=160) for a in hotspots],
        "recent_commits": [_artifact_summary(a, body_chars=200) for a in commits],
        "prs": [_artifact_summary(a, body_chars=300) for a in prs],
        "adrs": [_artifact_summary(a, body_chars=600) for a in adrs],
        "notes": [_artifact_summary(a, body_chars=600) for a in notes],
        "markers": [_artifact_summary(a, body_chars=200) for a in markers],
        "session_insights": session_insights,
    }
    return json.dumps(bundle, indent=2, default=str)


def _matches_area(art: Artifact, area: str) -> bool:
    if art.area == area:
        return True
    sig = art.signals or {}
    # Some artifacts (commits, PRs) carry an "areas" list under signals.
    if isinstance(sig.get("areas"), list) and area in sig["areas"]:
        return True
    return False


def _artifact_summary(art: Artifact, *, body_chars: int) -> dict:
    sig = art.signals or {}
    body = (art.body or "").strip().replace("\r\n", "\n")
    if len(body) > body_chars:
        body = body[:body_chars].rstrip() + "…"
    ts = art.ts.isoformat() if isinstance(art.ts, datetime) else str(art.ts)
    return {
        "id": art.id,
        "title": (art.title or "").strip()[:160],
        "ts": ts,
        "area": art.area,
        "body": body,
        "signals": _slim_signals(sig),
    }


_KEEP_SIGNAL_KEYS = {
    "is_bugfix", "is_revert", "files_changed", "path",
    # hotspot fields (the gatherer's actual field names — `commits_30d` was a
    # historical typo)
    "total_changes_30d", "total_changes_90d",
    "bugfix_count_30d", "bugfix_count_90d", "fix_ratio_90d",
    "recent_fix_prs", "recent_fix_commits", "co_changed_files",
    "owners",
    # commit / pr signals
    "tags", "areas", "merged", "author", "conv_type", "conv_scope",
    "fixes_issues", "reverts_sha", "is_merge", "coauthors",
    # pr-only
    "labels", "approvals", "change_requests", "merged_by",
}


def _slim_signals(sig: dict) -> dict:
    out: dict = {}
    for k in _KEEP_SIGNAL_KEYS:
        if k in sig:
            v = sig[k]
            # Cap any list to 12 items to avoid bloating the bundle.
            if isinstance(v, list) and len(v) > 12:
                v = v[:12] + ["…"]
            out[k] = v
    return out


def _read_session_insights(repo_root: Path) -> list[dict]:
    """Read raw/notes/session/*.md (ratchet outputs) for the bundle."""
    sd = raw_notes_dir(repo_root) / "session"
    if not sd.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(sd.glob("*.md"), key=lambda x: -x.stat().st_mtime)[:20]:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # Strip front-matter for the LLM (don't spend tokens on metadata).
        body = text
        if body.startswith("---\n"):
            end = body.find("\n---\n", 4)
            if end != -1:
                body = body[end + 5:]
        out.append({
            "session_id": p.stem,
            "body": body.strip()[:1200],
        })
    return out
