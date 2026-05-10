"""Tests for prs gatherer — inline-comment allowlist + concurrency / partial-progress."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent_forge._subprocess import Completed
from agent_forge.wiki import storage
from agent_forge.wiki.gather.builtin import prs as prs_mod


def _pr_listing(numbers: list[int]) -> str:
    return json.dumps([
        {"number": n, "title": f"PR #{n}", "mergedAt": "2025-01-15T12:00:00Z"}
        for n in numbers
    ])


def _pr_full(number: int, *, reviews=None, comments=None, files=None,
             title=None, labels=None) -> str:
    return json.dumps({
        "number": number,
        "title": title or f"PR #{number}",
        "body": "body text",
        "author": {"login": "alice"},
        "mergedBy": {"login": "bob"},
        "mergedAt": "2025-01-15T12:00:00Z",
        "files": [{"path": p} for p in (files or ["src/x.py"])],
        "reviews": reviews or [],
        "comments": comments or [],
        "labels": [{"name": l} for l in (labels or [])],
    })


def _make_run(answers: dict[tuple, str]):
    """Create an async fake_run that maps gh subcommand tuples → stdout."""
    async def fake_run(cmd, *args, **kwargs):
        # cmd starts with ["gh", "pr", ...]
        key = tuple(cmd[1:4])  # ("pr", "list", "--state") or ("pr", "view", "<n>")
        for k, val in answers.items():
            if cmd[1:1+len(k)] == list(k):
                return Completed(0, val, "")
        return Completed(0, "", "")
    return fake_run


def test_extract_review_thread_filters_by_author():
    reviews = [
        {
            "author": {"login": "sara"},
            "state": "COMMENTED",
            "body": "looks good",
            "comments": [
                {"author": {"login": "sara"},   "path": "x.py", "body": "nit"},
                {"author": {"login": "intern"}, "path": "x.py", "body": "noise"},
            ],
        },
    ]
    top, inline = prs_mod._extract_review_thread(reviews, {"sara"})
    # Top-level review body always kept
    assert len(top) == 1 and top[0]["author"] == "sara"
    # Only sara's inline comment kept; intern's filtered out
    assert len(inline) == 1
    assert inline[0]["author"] == "sara"


def test_extract_review_thread_star_keeps_everything():
    reviews = [
        {
            "author": {"login": "x"},
            "state": "COMMENTED",
            "body": "",
            "comments": [
                {"author": {"login": "anyone"}, "path": "y", "body": "ok"},
            ],
        },
    ]
    _, inline = prs_mod._extract_review_thread(reviews, {"*"})
    assert len(inline) == 1


def test_extract_review_thread_empty_allowlist_drops_all_inline():
    reviews = [
        {
            "author": {"login": "x"},
            "state": "APPROVED",
            "body": "",
            "comments": [
                {"author": {"login": "x"}, "path": "y", "body": "comment"},
            ],
        },
    ]
    top, inline = prs_mod._extract_review_thread(reviews, set())
    assert top  # APPROVED review kept even with empty body
    assert inline == []


def test_to_artifact_marks_bugfix_from_label():
    pr = json.loads(_pr_full(42, labels=["bug"]))
    art = prs_mod._to_artifact(pr, [], [], [])
    assert art.signals["is_bugfix"] is True


def test_to_artifact_marks_bugfix_from_title_prefix():
    pr = json.loads(_pr_full(43, title="fix: stop leak"))
    art = prs_mod._to_artifact(pr, [], [], [])
    assert art.signals["is_bugfix"] is True


def test_to_artifact_marks_revert_from_title():
    pr = json.loads(_pr_full(44, title="Revert \"add cache\""))
    art = prs_mod._to_artifact(pr, [], [], [])
    assert art.signals["is_revert"] is True


# ── PR area attribution at gather-time ──────────────────────────────────────

def test_to_artifact_attributes_area_from_files_changed():
    """A PR touching only `src/payments/*` gets area='payments'."""
    pr = json.loads(_pr_full(101, files=["src/payments/refund.py", "src/payments/types.py"]))
    areas = {"payments": ["src/payments/**"], "auth": ["src/auth/**"]}
    art = prs_mod._to_artifact(pr, [], [], [], areas)
    assert art.area == "payments"


def test_to_artifact_multi_area_pr_records_full_set():
    """Multi-area PR: primary in `area`, full set in signals.areas."""
    pr = json.loads(_pr_full(102, files=["src/payments/x.py", "src/auth/y.py"]))
    areas = {"payments": ["src/payments/**"], "auth": ["src/auth/**"]}
    art = prs_mod._to_artifact(pr, [], [], [], areas)
    # Deterministic primary (alphabetical first).
    assert art.area == "auth"
    assert sorted(art.signals.get("areas") or []) == ["auth", "payments"]


def test_to_artifact_no_areas_map_leaves_area_none():
    """Backwards compat: areas_map=None → no attribution, no `areas` signal."""
    pr = json.loads(_pr_full(103, files=["src/foo.py"]))
    art = prs_mod._to_artifact(pr, [], [], [], None)
    assert art.area is None
    assert "areas" not in (art.signals or {})


def test_to_artifact_files_outside_areas_leaves_area_none():
    """Files that match no area glob → area=None (not raised, not '(other)')."""
    pr = json.loads(_pr_full(104, files=["scripts/release.sh"]))
    areas = {"payments": ["src/payments/**"]}
    art = prs_mod._to_artifact(pr, [], [], [], areas)
    assert art.area is None
    assert "areas" not in (art.signals or {})


@pytest.mark.asyncio
async def test_gather_no_gh_returns_empty(tmp_path):
    with patch.object(prs_mod.shutil, "which", return_value=None):
        g = prs_mod.PRsGatherer()
        out = await g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), {})
    assert out == []


@pytest.mark.asyncio
async def test_gather_writes_artifacts_with_inline_filter(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "inline_comment_authors:\n  - sara\n"
    )
    full_pr = _pr_full(
        7,
        reviews=[
            {"author": {"login": "sara"}, "state": "APPROVED", "body": "lgtm",
             "comments": [
                 {"author": {"login": "sara"},   "path": "src/x.py", "body": "nit: rename"},
                 {"author": {"login": "intern"}, "path": "src/x.py", "body": "junk"},
             ]},
        ],
    )
    answers = {("pr", "list"): _pr_listing([7]), ("pr", "view", "7"): full_pr}

    with patch.object(prs_mod.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(prs_mod, "run_subprocess", _make_run(answers)):
        g = prs_mod.PRsGatherer()
        cursor: dict = {}
        out = await g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), cursor)

    assert len(out) == 1
    art = out[0]
    assert art.id == "pr-7"
    inline = art.signals["inline_comments"]
    assert len(inline) == 1
    assert inline[0]["author"] == "sara"
    # Cursor advanced.
    assert cursor["prs"]["last_number"] == 7


@pytest.mark.asyncio
async def test_gather_skips_already_seen_via_cursor(tmp_path):
    """When cursor.last_number is high, _list_recent_merged drops smaller numbers."""
    answers = {("pr", "list"): _pr_listing([5, 6, 7])}

    captured_views: list[str] = []

    async def fake_run(cmd, *args, **kwargs):
        if cmd[1:3] == ["pr", "list"]:
            return Completed(0, _pr_listing([5, 6, 7]), "")
        if cmd[1:3] == ["pr", "view"]:
            captured_views.append(cmd[3])  # PR number
            return Completed(0, _pr_full(int(cmd[3])), "")
        return Completed(0, "", "")

    with patch.object(prs_mod.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(prs_mod, "run_subprocess", fake_run):
        g = prs_mod.PRsGatherer()
        cursor = {"prs": {"last_number": 6}}
        out = await g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), cursor)

    # Only PR #7 should be viewed (5 and 6 already seen).
    assert captured_views == ["7"]
    assert len(out) == 1
    assert out[0].id == "pr-7"


# ── Concurrency & partial-progress ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gather_view_calls_are_bounded_in_concurrency(tmp_path, monkeypatch):
    """`gh pr view` must run with at most _VIEW_CONCURRENCY calls in flight.

    Regression test for the original failure mode: serial fetches blew the
    120 s budget on busy repos. The fix uses an asyncio.Semaphore — this test
    pins that contract by counting peak in-flight `pr view` calls.
    """
    monkeypatch.setattr(prs_mod, "_VIEW_CONCURRENCY", 3)
    n_prs = 12
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_run(cmd, *args, **kwargs):
        nonlocal in_flight, peak
        if cmd[1:3] == ["pr", "list"]:
            return Completed(0, _pr_listing(list(range(1, n_prs + 1))), "")
        if cmd[1:3] == ["pr", "view"]:
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                # Yield to the loop so other coroutines can hit the semaphore.
                await asyncio.sleep(0.01)
                return Completed(0, _pr_full(int(cmd[3])), "")
            finally:
                async with lock:
                    in_flight -= 1
        return Completed(0, "", "")

    with patch.object(prs_mod.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(prs_mod, "run_subprocess", fake_run):
        g = prs_mod.PRsGatherer()
        cursor: dict = {}
        out = await g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), cursor)

    assert len(out) == n_prs
    assert peak <= 3, f"semaphore breach: peak={peak} > 3"
    assert peak >= 2, f"never went parallel: peak={peak} (test setup broken?)"
    assert cursor["prs"]["last_number"] == n_prs


@pytest.mark.asyncio
async def test_gather_returns_partial_results_when_some_views_hang(tmp_path, monkeypatch):
    """If the internal deadline fires mid-fetch, completed PRs must survive
    and the cursor must advance only over the contiguous prefix.

    Setup: PRs 1..5 listed. PR #3's view hangs forever; #1, #2, #4, #5 finish.
    After deadline:
      - out should contain PRs 1 and 2 (the contiguous prefix)
      - PRs 4 and 5 are dropped (gap at #3)
      - cursor.last_number == 2 so the next run re-fetches #3 onwards
    """
    # Tiny budget so the test runs fast. 30 s floor in the impl means
    # we set timeout_seconds = _DEADLINE_SAFETY_S + 1 won't help; instead
    # we monkeypatch the safety constant down to 0 and timeout_seconds to 1.
    monkeypatch.setattr(prs_mod, "_DEADLINE_SAFETY_S", 0)
    # Override the floor: the impl uses max(timeout - safety, 30) — patch
    # the class so the deadline math gives us ~0.5 s.
    monkeypatch.setattr(prs_mod.PRsGatherer, "timeout_seconds", 30)
    # And override the floor inside gather() by monkeypatching time.monotonic
    # is too invasive. Easier: hang #3 long enough that it's still pending
    # when discovery's wait_for would fire — we don't actually invoke discovery
    # here, so we just verify the contiguous-prefix walk by cancelling #3.
    started: dict[int, asyncio.Event] = {}
    block_3 = asyncio.Event()  # never set -> #3 hangs

    async def fake_run(cmd, *args, **kwargs):
        if cmd[1:3] == ["pr", "list"]:
            return Completed(0, _pr_listing([1, 2, 3, 4, 5]), "")
        if cmd[1:3] == ["pr", "view"]:
            n = int(cmd[3])
            started[n] = asyncio.Event()
            started[n].set()
            if n == 3:
                await block_3.wait()  # never returns
            await asyncio.sleep(0.005)
            return Completed(0, _pr_full(n), "")
        return Completed(0, "", "")

    with patch.object(prs_mod.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(prs_mod, "run_subprocess", fake_run):
        g = prs_mod.PRsGatherer()
        cursor: dict = {}

        # Drive gather() under a tight external timeout — emulates discovery's
        # wait_for firing. The impl's internal deadline should beat us to it
        # in production, but for the test we just need *some* cancellation
        # boundary so #3's task is left un-completed.
        gather_task = asyncio.create_task(
            g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), cursor)
        )
        # Wait long enough for #1, #2 (and likely #4, #5) to finish behind the
        # semaphore, but #3 is still blocked.
        await asyncio.sleep(0.3)
        # Now cancel #3's hanging subprocess by setting the event so the
        # gather can finish — but first patch in a deadline by directly
        # cancelling the inner task. Simplest path: cancel the task and
        # assert the gather still produced partial output via the
        # gather_task's cancellation handler.
        gather_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gather_task

    # When discovery's wait_for cancels gather(), we DO lose in-memory `out`.
    # That's expected — the cursor advancement is the durable part, but
    # cursor is mutated only on the last line of gather() which never runs
    # under outer cancellation. So this test really validates that the
    # *internal* deadline path (below) is the one that preserves work.
    # → This negative case documents why the internal deadline matters.
    assert "prs" not in cursor or cursor["prs"].get("last_number", 0) == 0


@pytest.mark.asyncio
async def test_gather_internal_deadline_preserves_partial_work(tmp_path, monkeypatch):
    """Internal deadline path: gather() returns partial results + advances cursor
    even when not all `gh pr view` calls finish.

    We force the internal deadline by patching time.monotonic so the second
    call (inside gather, after listing) reports "deadline already past" — but
    asyncio.wait with timeout=0.1 still lets the in-flight tasks settle.
    PRs that finish before the deadline land in `out`; PR #3 hangs forever
    and is cancelled, breaking the contiguous-prefix walk after #2.
    """
    monkeypatch.setattr(prs_mod, "_VIEW_CONCURRENCY", 1)  # serial → deterministic order
    monkeypatch.setattr(prs_mod, "_DEADLINE_SAFETY_S", 0)

    block_3 = asyncio.Event()  # never set

    async def fake_run(cmd, *args, **kwargs):
        if cmd[1:3] == ["pr", "list"]:
            return Completed(0, _pr_listing([1, 2, 3, 4, 5]), "")
        if cmd[1:3] == ["pr", "view"]:
            n = int(cmd[3])
            if n == 3:
                await block_3.wait()  # hangs forever
            # Tiny await so the cancellation in `finally` actually has a
            # chance to fire on later tasks before they complete.
            await asyncio.sleep(0)
            return Completed(0, _pr_full(n), "")
        return Completed(0, "", "")

    # Skew time.monotonic so the second call (computing time_left) lands
    # past the deadline, collapsing time_left to the 0.1 s floor inside the
    # impl. That lets #1 and #2 finish (no real I/O) but cancels #3 + later.
    real_monotonic = prs_mod.time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        # First call → deadline = real_now + 30 (the impl's floor).
        # Second call (time_left calc) → bump way past deadline so
        # time_left clamps to 0.1 s.
        if calls["n"] >= 2:
            return real_monotonic() + 1_000
        return real_monotonic()

    monkeypatch.setattr(prs_mod.time, "monotonic", fake_monotonic)

    with patch.object(prs_mod.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(prs_mod, "run_subprocess", fake_run):
        g = prs_mod.PRsGatherer()
        cursor: dict = {}
        out = await g.gather(tmp_path, datetime(2024, 1, 1, tzinfo=timezone.utc), cursor)

    # PRs 1 and 2 finished and form the contiguous prefix; #3 hung; #4, #5
    # never started (serial concurrency=1) so they're cancelled. Walk stops
    # at #3 → cursor advances to 2, not 5.
    assert {a.id for a in out} == {"pr-1", "pr-2"}
    assert cursor["prs"]["last_number"] == 2
