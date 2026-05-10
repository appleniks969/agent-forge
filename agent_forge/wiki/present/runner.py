"""
wiki/present.py — render gathered/curated knowledge into a system-prompt section.

Stage 3 of the compounding loop. Pure mechanical: read files from
``.agent-forge/curated/`` (preferred) or fall back to ``.agent-forge/raw/``,
trim each chunk to a per-section byte budget, glue with markdown headers,
return one string suitable for ``SystemPrompt.register(SectionName.WIKI, …)``.

No LLM call here — that lives in ``compile/``. Picking is heuristic
(top-N by recency / hotness / file mtime). Prompt-aware ranking is the
MVP-3 follow-up; the function signature is shaped so adding it later is
non-breaking (just one more optional kwarg).

Reads:    .agent-forge/curated/  (when present)
          .agent-forge/raw/cache/  (fallback for hotspots, recent commits, notes)
Writes:   nothing — returns a string

Public:   build_wiki_section(repo_root, *, budget=8000, user_prompt=None) -> str | None
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .._noise import is_noisy_path
from ..storage import (
    areas_for_paths, curated_dir, list_artifacts, load_contexts,
    raw_cache_dir, raw_notes_dir,
)

# ── Tunables ──────────────────────────────────────────────────────────────────

# Default total budget for the WIKI section (bytes, not tokens — string length).
# Roughly ~2K tokens, deliberately well under the system-prompt budget.
_DEFAULT_BUDGET = 8000

# Per-subsection caps. Sum exceeds budget on purpose; we trim greedy-then-stop.
_CAP_ONBOARDING   = 1500
_CAP_HOTSPOTS     = 1800
_CAP_ADRS         = 1500
_CAP_NOTES        = 1500
_CAP_RECENT       = 1200
_CAP_AREA_EACH    = 800
_CAP_CONVENTIONS  = 3500   # repo_file artifacts (AGENTS.md, CONTRIBUTING.md, README.md)
_CAP_CONV_PER_DOC = 2200   # per-doc cap inside conventions section
_CAP_REVERTS      = 600    # short list, high signal
_CAP_PRS          = 1400   # notable PRs by review-comment density

# Repo doc files we surface as "project conventions" (case-sensitive titles
# match what repo_files.py writes).
_CONVENTION_DOCS = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md")


# ── Public entry point ────────────────────────────────────────────────────────

def build_wiki_section(
    repo_root: Path | str,
    *,
    budget: int = _DEFAULT_BUDGET,
    user_prompt: str | None = None,  # noqa: ARG001 — reserved for MVP-3 ranking
) -> str | None:
    """Return the WIKI section string, or None if nothing is available.

    Order of preference:
      1. ``.agent-forge/curated/`` (compiled by MVP-3) — narrative, ranked.
      2. ``.agent-forge/raw/`` (gathered by MVP-1) — hotspots + notes summary.

    The string is capped at ``budget`` bytes. ``user_prompt`` is accepted but
    ignored in MVP-2; MVP-3 will use it for prompt-aware ranking.
    """
    root = Path(repo_root)
    curated = curated_dir(root)
    raw_cache = raw_cache_dir(root)

    sections: list[str] = []

    if curated.exists() and any(curated.iterdir()):
        sections = _render_from_curated(curated)
    elif raw_cache.exists() and any(raw_cache.iterdir()):
        sections = _render_from_raw(root)
    else:
        return None

    sections = [s for s in sections if s and s.strip()]
    if not sections:
        return None

    body = _join_under_budget(sections, budget)
    if not body.strip():
        return None
    return f"## Repository wiki (auto-curated)\n\n{body}"


# ── Curated renderer (preferred) ──────────────────────────────────────────────

def _render_from_curated(curated: Path) -> list[str]:
    """Render the WIKI section from .agent-forge/curated/ files (MVP-3 output).

    Expected files (all optional):
      onboarding.md
      hotspots.md
      adrs.md
      per_area/*.md
    """
    out: list[str] = []
    out.append(_read_capped(curated / "onboarding.md", _CAP_ONBOARDING, header="### Onboarding"))
    out.append(_read_capped(curated / "hotspots.md", _CAP_HOTSPOTS, header="### Hot files"))
    out.append(_read_capped(curated / "adrs.md", _CAP_ADRS, header="### Decisions (ADRs)"))

    per_area = curated / "per_area"
    if per_area.is_dir():
        # One short slice per area, newest first by mtime.
        area_files = sorted(per_area.glob("*.md"), key=lambda p: -p.stat().st_mtime)
        for af in area_files[:6]:  # cap at 6 areas to keep the section bounded
            text = _safe_read(af)
            if not text:
                continue
            slice_ = _truncate(text, _CAP_AREA_EACH)
            out.append(f"### Area: {af.stem}\n{slice_}")

    return [s for s in out if s]


# ── Raw renderer (fallback when curated/ is empty) ────────────────────────────

def _render_from_raw(repo_root: Path) -> list[str]:
    """Render a heuristic wiki straight from raw/cache/ artifacts.

    This is the MVP-2 dumb path: hotspots + recent bugfix commits + notes.
    No ranking, no synthesis — just enough that the agent has *something*
    before the user runs MVP-3 compile.
    """
    out: list[str] = []

    # Project conventions — surface AGENTS.md / CONTRIBUTING.md / README.md
    # bodies first. These are the highest-signal artifacts: project rules the
    # team has already written down. The agent should see them before anything
    # else so it doesn't violate house-style conventions.
    conv = _render_conventions(repo_root)
    if conv:
        out.append(conv)

    # Hot files — ordered by 30-day total-changes then bugfix count, with
    # auto-edited / generated paths filtered out (CHANGELOGs, lockfiles,
    # *.generated.*, .github/, node_modules/, …). When contexts.yaml exists,
    # group by declared area instead of one flat list.
    hotspots_all = list(list_artifacts(repo_root, kind="hotspot"))
    hotspots = sorted(
        (
            h for h in hotspots_all
            if not is_noisy_path((h.signals or {}).get("path", ""))
        ),
        key=lambda a: (
            -int(a.signals.get("total_changes_30d", 0) or 0),
            -int(a.signals.get("bugfix_count_30d", 0) or 0),
        ),
    )[:15]  # take 15 so per-area grouping has enough to fill multiple buckets
    if hotspots:
        areas_map, _inline = load_contexts(repo_root)
        if areas_map:
            section = _render_hotspots_per_area(hotspots, areas_map)
        else:
            section = _render_hotspots_flat(hotspots[:10])
        out.append(_truncate(section, _CAP_HOTSPOTS))

    # Recent bugfix commits — durable signal of where the team is fighting.
    commits = list(list_artifacts(repo_root, kind="commit"))
    bugfixes = sorted(
        [c for c in commits if (c.signals or {}).get("is_bugfix")],
        key=lambda c: c.ts,
        reverse=True,
    )[:8]
    if bugfixes:
        lines = ["### Recent bug fixes"]
        for c in bugfixes:
            ts = c.ts.strftime("%Y-%m-%d") if isinstance(c.ts, datetime) else str(c.ts)
            title = (c.title or "").strip().splitlines()[0][:80] if c.title else c.id
            lines.append(f"- {ts} — {title}")
        out.append(_truncate("\n".join(lines), _CAP_RECENT))

    # Recently reverted — "we tried this and undid it" is high-signal context
    # for an agent about to propose similar changes.
    reverts = sorted(
        list(list_artifacts(repo_root, kind="revert")),
        key=lambda a: a.ts,
        reverse=True,
    )[:6]
    if reverts:
        lines = ["### Recently reverted (avoid re-introducing)"]
        for r in reverts:
            ts = r.ts.strftime("%Y-%m-%d") if isinstance(r.ts, datetime) else str(r.ts)
            title = (r.title or "").strip().splitlines()[0][:90] if r.title else r.id
            # Strip the "Revert " prefix git always adds — it's redundant under the header.
            if title.lower().startswith("revert "):
                title = title[7:].lstrip('"').rstrip('"')
            lines.append(f"- {ts} — {title}")
        out.append(_truncate("\n".join(lines), _CAP_REVERTS))

    # Notable PRs — ranked by review-comment density (a proxy for "design
    # discussion happened here"). High-signal for understanding *why* code
    # looks the way it does.
    prs = list(list_artifacts(repo_root, kind="pr"))
    def _pr_discussion_score(p) -> int:
        s = p.signals or {}
        return (
            len(s.get("review_comments") or [])
            + len(s.get("inline_comments") or [])
            + len(s.get("issue_comments") or [])
            + 2 * len(s.get("change_requests") or [])  # change_requests weighted higher
        )
    notable = sorted(
        ((p, _pr_discussion_score(p)) for p in prs),
        key=lambda t: (-t[1], -t[0].ts.timestamp() if isinstance(t[0].ts, datetime) else 0),
    )
    notable = [p for p, score in notable if score >= 2][:6]
    if notable:
        lines = ["### Notable PRs (high discussion)"]
        for p in notable:
            ts = p.ts.strftime("%Y-%m") if isinstance(p.ts, datetime) else str(p.ts)
            title = (p.title or "").strip().splitlines()[0][:90] if p.title else p.id
            score = _pr_discussion_score(p)
            lines.append(f"- {ts} — {title} _({score} comments)_")
        out.append(_truncate("\n".join(lines), _CAP_PRS))

    # Notes — hand-curated, always include up to budget.
    note_arts = sorted(
        list(list_artifacts(repo_root, kind="note")),
        key=lambda a: a.ts,
        reverse=True,
    )[:10]
    if note_arts:
        lines = ["### Notes"]
        for n in note_arts:
            title = (n.title or n.id).strip()
            body = (n.body or "").strip().splitlines()
            first_para = " ".join(body)[:200]
            lines.append(f"- **{title}** — {first_para}")
        out.append(_truncate("\n".join(lines), _CAP_NOTES))

    # Session insights from ratchet (raw/notes/session/*.md) — the loop closure.
    session_dir = raw_notes_dir(repo_root) / "session"
    if session_dir.is_dir():
        recents = sorted(
            session_dir.glob("*.md"),
            key=lambda p: -p.stat().st_mtime,
        )[:5]
        if recents:
            lines = ["### Recent session insights"]
            for p in recents:
                text = _safe_read(p)
                if not text:
                    continue
                # First non-blank, non-header line, capped.
                snippet = _first_useful_line(text)[:200]
                lines.append(f"- {snippet}")
            if len(lines) > 1:
                out.append(_truncate("\n".join(lines), _CAP_NOTES))

    return [s for s in out if s]


# ── Internals ─────────────────────────────────────────────────────────────────

def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[: cap - 32].rstrip()
    return cut + "\n…[truncated]"


def _read_capped(path: Path, cap: int, *, header: str | None = None) -> str:
    text = _safe_read(path)
    if not text or not text.strip():
        return ""
    text = _truncate(text.strip(), cap)
    if header and not text.lstrip().startswith("#"):
        return f"{header}\n{text}"
    return text


def _join_under_budget(sections: list[str], budget: int) -> str:
    """Greedy concat with a hard byte cap. Sections in priority order."""
    out: list[str] = []
    used = 0
    sep = "\n\n"
    for s in sections:
        s = s.strip()
        if not s:
            continue
        cost = len(s) + (len(sep) if out else 0)
        if used + cost > budget:
            # If we have nothing yet, take the first section truncated to budget.
            if not out and budget > 64:
                out.append(_truncate(s, budget))
                used = budget
            break
        out.append(s)
        used += cost
    return sep.join(out)


def _first_useful_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            return line
    return ""


def _render_conventions(repo_root: Path) -> str:
    """Render AGENTS.md / CONTRIBUTING.md / README.md bodies as a single
    "Project conventions" section using *section-aware* compression.

    Strategy: we already have these on disk as ``repo_file`` artifacts (body =
    full text). Naive byte truncation cuts contributor rules in half — bad.
    Instead we extract a "skeleton": every ``##`` / ``###`` heading plus the
    first 3 bullet lines (or 2 non-empty paragraph lines) under it. The result
    preserves ALL section headers — so the agent sees every rule the team has
    written down — at ~1/5 the byte cost of the raw file.
    """
    by_title = {a.title: a for a in list_artifacts(repo_root, kind="repo_file")}
    pieces: list[str] = []
    for name in _CONVENTION_DOCS:
        art = by_title.get(name)
        if not art or not (art.body or "").strip():
            continue
        # Adaptive compression: try with 3 bullets/section first; if that
        # overflows the per-doc cap and *truncates* (drops headings off the
        # bottom), retry with tighter budgets so all section headers survive.
        # Section headings are the high-signal output here — losing the
        # CRITICAL "Git Rules for Parallel Agents" section is a real risk.
        skeleton = _markdown_skeleton(art.body, max_chars=_CAP_CONV_PER_DOC)
        if "[truncated]" in skeleton:
            for bullets, prose in ((2, 1), (1, 1), (0, 0)):
                tighter = _markdown_skeleton(
                    art.body,
                    max_chars=_CAP_CONV_PER_DOC,
                    bullet_budget=bullets,
                    prose_budget=prose,
                )
                if "[truncated]" not in tighter:
                    skeleton = tighter
                    break
                skeleton = tighter   # use the tightest even if still truncated
        if not skeleton.strip():
            continue
        # If the skeleton is a strict subset of the body, append a "see file" hint.
        if len(skeleton) < len(art.body) - 64:
            skeleton = skeleton.rstrip() + f"\n_(skeleton — full file: {name})_"
        pieces.append(f"#### {name}\n{skeleton}")
    if not pieces:
        return ""
    section = "### Project conventions\n" + "\n\n".join(pieces)
    return _truncate(section, _CAP_CONVENTIONS)


def _markdown_skeleton(
    body: str,
    *,
    max_chars: int,
    bullet_budget: int = 3,
    prose_budget: int = 2,
) -> str:
    """Compress a markdown doc to ``heading + first few bullets`` per section.

    Algorithm:
      - Walk lines top-to-bottom.
      - The leading ``# title`` is kept verbatim.
      - For each ``## `` / ``### `` heading: keep the heading line, then the
        first ``bullet_budget`` bullet lines (``- `` or ``* `` or numbered)
        under it, OR if no bullets, the first ``prose_budget`` non-empty /
        non-code prose lines.
      - Skip code fences and HTML.
      - Stop once the running output exceeds ``max_chars``; append a marker.

    Setting ``bullet_budget=0, prose_budget=0`` produces a *headings-only*
    skeleton — useful as a fallback for huge files where heading coverage
    matters more than per-section detail.

    This loses examples and detail-prose but keeps the *structure* of the rules,
    which is what an agent needs to know "what's covered" before drilling in.
    """
    out: list[str] = []
    lines = body.splitlines()
    i = 0
    in_code = False
    kept_under_section = 0          # bullets/prose lines kept under current section
    saw_bullet = False              # within current section, have we seen a bullet?

    def _is_bullet(s: str) -> bool:
        t = s.lstrip()
        if not t:
            return False
        if t.startswith(("- ", "* ", "+ ")):
            return True
        # numbered list: "1. ", "12) "
        head = t.split(maxsplit=1)[0] if t.split() else ""
        return bool(head[:-1].isdigit() and head[-1:] in ".)")

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Code fence toggling — never include code in the skeleton.
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue

        # Top-level title (single # at file start) — keep verbatim once.
        if stripped.startswith("# ") and not any(line.startswith("# ") for line in (l.strip() for l in lines[:i])):
            out.append(raw)
            out.append("")
            i += 1
            continue

        # Section heading (## or ###) — always kept; resets per-section budgets.
        if stripped.startswith(("## ", "### ", "#### ")):
            out.append(raw)
            kept_under_section = 0
            saw_bullet = False
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        # Within a section: keep up to N bullets OR up to M prose lines.
        if _is_bullet(stripped):
            if not saw_bullet:
                # switching from prose to bullets — reset counter
                kept_under_section = 0
                saw_bullet = True
            if kept_under_section < bullet_budget:
                # cap individual bullet length so a single mega-bullet doesn't blow budget
                trimmed = raw if len(raw) <= 200 else raw[:200].rstrip() + "…"
                out.append(trimmed)
                kept_under_section += 1
        elif not saw_bullet:
            if kept_under_section < prose_budget:
                trimmed = raw if len(raw) <= 200 else raw[:200].rstrip() + "…"
                out.append(trimmed)
                kept_under_section += 1
        # else: prose after we already have bullets — drop.

        # Early stop on budget.
        if sum(len(s) + 1 for s in out) > max_chars:
            out.append("…[truncated]")
            break
        i += 1

    # Drop trailing blank lines.
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ── Hot-files renderers (flat or per-area) ────────────────────────────────────

def _render_hotspots_flat(hotspots: list) -> str:
    lines = ["### Hot files (last 30 days)"]
    for h in hotspots:
        lines.append(_format_hotspot_line(h))
    return "\n".join(lines)


def _render_hotspots_per_area(hotspots: list, areas_map: dict[str, list[str]]) -> str:
    """Group hot files by declared area; uncategorised goes under '(other)'.

    Limits each area to 5 entries so a single hot area can't crowd out the rest.
    Empty areas are dropped from the rendering.
    """
    buckets: dict[str, list] = {a: [] for a in areas_map}
    buckets["(other)"] = []
    for h in hotspots:
        path = (h.signals or {}).get("path") or ""
        matched = areas_for_paths([path], areas_map) if path else set()
        if matched:
            # An entry can match multiple areas; attribute to the first deterministically.
            first = sorted(matched)[0]
            buckets[first].append(h)
        else:
            buckets["(other)"].append(h)

    lines = ["### Hot files by area (last 30 days)"]
    # Sort areas by their max activity so the most-active area shows first.
    def _area_score(name: str) -> int:
        items = buckets.get(name) or []
        return -max(
            (int((i.signals or {}).get("total_changes_30d", 0) or 0) for i in items),
            default=0,
        )
    for area in sorted(buckets, key=_area_score):
        items = buckets[area]
        if not items:
            continue
        lines.append("")
        lines.append(f"**{area}**")
        for h in items[:5]:
            lines.append(_format_hotspot_line(h))
    return "\n".join(lines)


def _format_hotspot_line(h) -> str:
    sig = h.signals or {}
    changes = sig.get("total_changes_30d", 0) or 0
    bugs = sig.get("bugfix_count_30d", 0) or 0
    owners_list = (sig.get("owners") or [])[:3]
    path = sig.get("path") or h.title or h.id
    bits = [f"{changes} changes"]
    if bugs:
        bits.append(f"{bugs} bugfix")
    if owners_list:
        bits.append(f"owners: {', '.join(owners_list)}")
    return f"- `{path}` — {'; '.join(bits)}"
