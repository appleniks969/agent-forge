"""
gather/builtin/prs.py — pull merged PRs via the `gh` CLI.

For each merged PR we capture title, body, author, mergedBy, files changed,
top-level review comments (always), and inline review comments (filtered
by the `inline_comment_authors` allowlist in contexts.yaml — empty list
means top-level only).

Cursor key: `prs.last_number`. First run uses the `since` datetime via
`gh pr list --search "merged:>=YYYY-MM-DD"`.

Gracefully no-ops if `gh` isn't installed or auth fails — logs to
.gather.log and returns []. Other gatherers continue.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_forge._subprocess import run as run_subprocess

from ...storage import areas_for_paths, load_contexts
from ...types import Artifact, Gatherer, Source

_PR_LIST_LIMIT = 1000     # per-run cap on the index call (gh paginates internally)
_VIEW_CONCURRENCY = 5     # parallel `gh pr view` calls (under GitHub secondary rate limit)
_DEADLINE_SAFETY_S = 30   # return partial work this many seconds before discovery's wait_for fires
_FIELDS_LIST = "number,title,mergedAt"
_FIELDS_VIEW = (
    "number,title,body,author,mergedBy,mergedAt,baseRefName,headRefName,"
    "files,reviews,comments,labels"
)


async def _gh(args: list[str], cwd: Path, timeout: float = 30.0):
    return await run_subprocess(["gh", *args], cwd=str(cwd), timeout=timeout)


async def _list_recent_merged(
    repo_root: Path,
    since: datetime,
    last_number: int | None,
) -> list[dict]:
    """Return [{number, title, mergedAt}, ...] for merged PRs to consider."""
    search = f"merged:>={since.strftime('%Y-%m-%d')}"
    res = await _gh(
        ["pr", "list", "--state", "merged", "--limit", str(_PR_LIST_LIMIT),
         "--search", search, "--json", _FIELDS_LIST],
        repo_root,
    )
    if res.returncode != 0:
        return []
    try:
        prs = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if last_number is not None:
        prs = [p for p in prs if p.get("number", 0) > last_number]
    # Newest first → reverse so cursor advances cleanly.
    prs.sort(key=lambda p: p.get("number", 0))
    return prs


async def _view_pr(repo_root: Path, number: int) -> dict | None:
    res = await _gh(
        ["pr", "view", str(number), "--json", _FIELDS_VIEW], repo_root,
    )
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return None


def _filter_comments(
    comments: list[dict],
    inline_allow: set[str],
) -> list[dict]:
    """Filter `gh`'s comments list by author allowlist.

    `gh pr view --json comments` returns *issue-style* PR comments (top-level
    discussion thread). They're always kept; the allowlist gates *inline*
    review comments which arrive nested under reviews[].comments — handled
    in `_extract_review_thread()`.
    """
    return [
        {
            "author": (c.get("author") or {}).get("login") or "unknown",
            "body": c.get("body") or "",
            "createdAt": c.get("createdAt"),
        }
        for c in comments
    ]


def _extract_review_thread(
    reviews: list[dict],
    inline_allow: set[str],
) -> tuple[list[dict], list[dict]]:
    """Return (top_level_reviews, filtered_inline_comments)."""
    top: list[dict] = []
    inline: list[dict] = []
    star = "*" in inline_allow
    for r in reviews:
        author = (r.get("author") or {}).get("login") or "unknown"
        state = r.get("state") or ""
        body = r.get("body") or ""
        if body.strip() or state in ("APPROVED", "CHANGES_REQUESTED"):
            top.append({
                "author": author,
                "state": state,
                "body": body,
                "submittedAt": r.get("submittedAt"),
            })
        # Inline comments — gh exposes them under r["comments"] when present.
        for c in r.get("comments") or []:
            c_author = (c.get("author") or {}).get("login") or "unknown"
            if not (star or c_author in inline_allow):
                continue
            inline.append({
                "author": c_author,
                "path": c.get("path"),
                "body": c.get("body") or "",
                "createdAt": c.get("createdAt"),
            })
    return top, inline


def _to_artifact(pr: dict, top_reviews: list[dict],
                 inline_comments: list[dict],
                 issue_comments: list[dict],
                 areas_map: dict[str, list[str]] | None = None) -> Artifact:
    number = pr.get("number")
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    merged_at = pr.get("mergedAt")
    try:
        ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00")) if merged_at else datetime.now(tz=timezone.utc)
    except (ValueError, AttributeError):
        ts = datetime.now(tz=timezone.utc)

    files = [f.get("path") for f in (pr.get("files") or []) if f.get("path")]
    labels = [l.get("name") for l in (pr.get("labels") or []) if l.get("name")]
    author = (pr.get("author") or {}).get("login") or "unknown"
    merged_by = (pr.get("mergedBy") or {}).get("login")

    approvals = [r["author"] for r in top_reviews if r.get("state") == "APPROVED"]
    change_reqs = [r["author"] for r in top_reviews if r.get("state") == "CHANGES_REQUESTED"]

    signals: dict = {
        "author": author,
        "merged_by": merged_by,
        "files_changed": files,
        "labels": labels,
        "approvals": approvals,
        "change_requests": change_reqs,
        "review_comments": top_reviews,
        "inline_comments": inline_comments,
        "issue_comments": issue_comments,
    }

    # Lightweight bug-fix signal — purely from labels + title prefix.
    title_lc = title.lower()
    if any(label.lower() in {"bug", "defect", "regression", "production-incident", "hotfix"} for label in labels):
        signals["is_bugfix"] = True
    if title_lc.startswith(("fix:", "fix(", "bugfix:", "hotfix:", "revert ", "revert(")):
        signals["is_bugfix"] = True
    if title_lc.startswith("revert "):
        signals["is_revert"] = True

    # Area attribution from files_changed × contexts.yaml. Multi-area PRs keep
    # the full set under signals.areas; the top `area` field picks the
    # deterministic primary so per-area pages can find them.
    primary_area: str | None = None
    if files and areas_map:
        matched = areas_for_paths(files, areas_map)
        if matched:
            primary_area = sorted(matched)[0]
            if len(matched) > 1:
                signals["areas"] = sorted(matched)

    return Artifact(
        id=f"pr-{number}",
        kind="pr",
        source=Source.BUILTIN,
        title=title,
        body=body,
        ts=ts,
        area=primary_area,
        signals=signals,
    )


class PRsGatherer(Gatherer):
    """Gather merged PRs via `gh`.

    Workload is O(N) `gh pr view` calls — each a network round-trip. To keep
    a fixed wall-clock budget useful on busy repos we:

      * Run up to ``_VIEW_CONCURRENCY`` views in parallel via an
        ``asyncio.Semaphore``.
      * Use an *internal* deadline ``_DEADLINE_SAFETY_S`` short of
        ``timeout_seconds`` so we return partial results cleanly instead of
        being cancelled by ``discovery.run_gather``'s ``asyncio.wait_for``
        (which would discard the in-flight ``out`` list and leave the cursor
        un-advanced — re-fetching the same PRs forever).
      * Advance ``cursor['prs']['last_number']`` only over the **longest
        contiguous prefix** of completed PRs in ascending PR-number order.
        Anything past the first un-completed task is dropped so the next run
        re-fetches the gap rather than skipping it.
    """
    name = "prs"
    # Budget scales with PR count: a busy monorepo can have ~1000 merged PRs/yr.
    # At ~0.6 s/view warm and concurrency 5, ~1000 PRs takes ~120 s; 600 s leaves
    # headroom for slow networks and gh secondary rate-limit backoff.
    timeout_seconds = 600

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        if not shutil.which("gh"):
            return []
        # Fast bail if we're not in a recognised repo (gh prints to stderr).
        # We don't fail the whole gather — just no-op cleanly.
        my_cursor = cursor.get(self.name) or {}
        last_number: int | None = my_cursor.get("last_number")

        listing = await _list_recent_merged(repo_root, since, last_number)
        if not listing:
            return []

        # Drop entries with no number up front so listing/tasks line up 1:1
        # in the contiguous-prefix walk below.
        listing = [e for e in listing if isinstance(e.get("number"), int)]
        if not listing:
            return []

        areas_map, inline_allow = load_contexts(repo_root)

        # Internal deadline — we want to surrender control before
        # discovery.run_gather's asyncio.wait_for cancels us, so partial
        # results survive and the cursor advances over what we did fetch.
        deadline = time.monotonic() + max(self.timeout_seconds - _DEADLINE_SAFETY_S, 30)
        sem = asyncio.Semaphore(_VIEW_CONCURRENCY)

        async def _bounded_view(num: int) -> dict | None:
            async with sem:
                return await _view_pr(repo_root, num)

        tasks = [asyncio.create_task(_bounded_view(e["number"])) for e in listing]
        try:
            time_left = max(deadline - time.monotonic(), 0.1)
            await asyncio.wait(tasks, timeout=time_left)
        finally:
            # Cancel anything still pending so we don't leak gh subprocesses.
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Drain cancellations so cancelled() / done() are reliable below.
            await asyncio.gather(*tasks, return_exceptions=True)

        # Walk the longest contiguous prefix of completed tasks (in ascending
        # PR-number order). The first un-completed task stops the walk so
        # cursor.last_number never advances past a gap — the next run will
        # pick up exactly where we left off.
        out: list[Artifact] = []
        highest = last_number or 0
        for entry, task in zip(listing, tasks, strict=True):
            if task.cancelled() or not task.done():
                break
            try:
                full = task.result()
            except Exception:
                # Mirrors the old `if full is None: continue` — skip a broken
                # PR but keep walking; cursor stays at the last good number.
                continue
            if full is None:
                continue
            number = full.get("number") or entry["number"]
            top, inline = _extract_review_thread(full.get("reviews") or [], inline_allow)
            issue_comments = _filter_comments(full.get("comments") or [], inline_allow)
            out.append(_to_artifact(full, top, inline, issue_comments, areas_map))
            if isinstance(number, int) and number > highest:
                highest = number

        cursor[self.name] = {**my_cursor, "last_number": highest}
        return out
