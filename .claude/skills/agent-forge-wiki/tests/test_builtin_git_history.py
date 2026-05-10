"""Tests for git_history gatherer — classification + cursor advancement.

All `git` calls are mocked via patch on `run_subprocess` (the imported
alias inside the module) so tests don't need a real git repo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent_forge._subprocess import Completed
from wiki.gather.builtin import git_history


def test_classify_detects_conventional_commits():
    sig = git_history._classify("fix: stop double-charge", "")
    assert sig.get("conv_type") == "fix"
    assert sig.get("is_bugfix") is True


def test_classify_detects_revert_with_sha():
    sig = git_history._classify(
        "Revert \"Add caching\"",
        "This reverts commit abcdef1234567890.\n",
    )
    assert sig["is_revert"] is True
    assert sig["reverts_sha"].startswith("abcdef")


def test_classify_extracts_issue_refs():
    sig = git_history._classify("fix(api): null guard", "Fixes #123, closes #456")
    assert sig["fixes_issues"] == [123, 456]


def test_parse_log_output_handles_one_record():
    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    text = (
        f"abc123{f}parentsha{f}sara{f}sara@x.io{f}1700000000{f}fix: x{f}body{f}coauth1{r}"
    )
    parsed = git_history._parse_log_output(text)
    assert parsed[0]["sha"] == "abc123"
    assert parsed[0]["author_name"] == "sara"
    assert parsed[0]["coauthors"] == ["coauth1"]


@pytest.mark.asyncio
async def test_gather_no_git_returns_empty(tmp_path):
    with patch.object(git_history.shutil, "which", return_value=None):
        g = git_history.GitHistoryGatherer()
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    assert out == []


@pytest.mark.asyncio
async def test_gather_emits_commit_and_revert_artifacts(tmp_path):
    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    log_text = (
        # newest first: revert
        f"sha2{f}sha1{f}sara{f}sara@x.io{f}1700001000{f}Revert \"Add cache\"{f}"
        f"This reverts commit sha1abc.{f}{r}"
        # then a regular fix commit
        f"sha1{f}sha0{f}marcus{f}m@x.io{f}1700000000{f}fix: bug{f}body{f}{r}"
    )

    calls: list[list[str]] = []

    async def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            return Completed(0, log_text, "")
        if cmd[:2] == ["git", "diff-tree"]:
            return Completed(0, "src/x.py\n", "")
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        cursor: dict = {}
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), cursor)

    kinds = sorted(a.kind for a in out)
    # 2 commits + 1 revert (only the revert commit emits both)
    assert kinds.count("commit") == 2
    assert kinds.count("revert") == 1
    # Cursor advanced to the newest commit (sha2).
    assert cursor["git_history"]["last_sha"] == "sha2"
    # files_changed populated by `git show`.
    assert any(a.signals.get("files_changed") == ["src/x.py"] for a in out)


@pytest.mark.asyncio
async def test_gather_uses_cursor_for_subsequent_runs(tmp_path):
    """When cursor.last_sha is set, the rev range becomes <sha>..HEAD."""
    captured: dict[str, list[str]] = {"log_args": []}

    async def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            captured["log_args"] = cmd
            return Completed(0, "", "")  # no new commits
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        cursor = {"git_history": {"last_sha": "deadbeef"}}
        await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), cursor)

    assert "deadbeef..main" in captured["log_args"]


# ── Area attribution at gather-time ──────────────────────────────────────────
# Per-area wiki pages depend on commits being tagged with `area` at gather
# time. Without these tests, that attribution can silently break (which is
# exactly what happened on first build → all per-area pages were empty).

@pytest.mark.asyncio
async def test_gather_attributes_area_from_files_changed(tmp_path):
    """A commit touching `src/payments/x.py` gets area='payments' when
    contexts.yaml declares `payments: [src/payments/**]`."""
    # Write contexts.yaml so areas_for_paths can find it.
    af = tmp_path / ".agent-forge"
    af.mkdir()
    (af / "contexts.yaml").write_text(
        "areas:\n"
        "  payments:\n"
        "    paths:\n"
        "      - \"src/payments/**\"\n"
        "  auth:\n"
        "    paths:\n"
        "      - \"src/auth/**\"\n",
        encoding="utf-8",
    )

    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    log_text = (
        f"shaA{f}sha0{f}sara{f}s@x.io{f}1700000000{f}feat: refund{f}body{f}{r}"
        f"shaB{f}sha0{f}marcus{f}m@x.io{f}1700001000{f}fix: oauth{f}body{f}{r}"
    )

    diff_for_sha = {
        "shaA": "src/payments/refund.py\nsrc/payments/types.py\n",
        "shaB": "src/auth/oauth.py\n",
    }

    async def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            return Completed(0, log_text, "")
        if cmd[:2] == ["git", "diff-tree"]:
            sha = cmd[-1]
            return Completed(0, diff_for_sha.get(sha, ""), "")
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    by_id = {a.id: a for a in out if a.kind == "commit"}
    assert by_id["commit-shaA"].area == "payments"
    assert by_id["commit-shaB"].area == "auth"


@pytest.mark.asyncio
async def test_gather_multi_area_commit_records_full_set(tmp_path):
    """A commit spanning two areas records the primary in `area` and the full
    set under signals.areas (so per-area filters can find it from either side)."""
    af = tmp_path / ".agent-forge"
    af.mkdir()
    (af / "contexts.yaml").write_text(
        "areas:\n"
        "  payments:\n"
        "    paths: [\"src/payments/**\"]\n"
        "  auth:\n"
        "    paths: [\"src/auth/**\"]\n",
        encoding="utf-8",
    )

    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    log_text = f"shaC{f}sha0{f}sara{f}s@x.io{f}1700000000{f}feat: cross-area{f}body{f}{r}"

    async def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            return Completed(0, log_text, "")
        if cmd[:2] == ["git", "diff-tree"]:
            return Completed(0, "src/payments/x.py\nsrc/auth/y.py\n", "")
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    [art] = [a for a in out if a.kind == "commit"]
    # Primary area is deterministic (alphabetical first match).
    assert art.area == "auth"
    # Full set is preserved under signals.
    assert sorted(art.signals.get("areas") or []) == ["auth", "payments"]


@pytest.mark.asyncio
async def test_gather_no_contexts_yaml_leaves_area_none(tmp_path):
    """No contexts.yaml → no attribution; commits still gather but `area` is
    None and signals.areas is absent. (Backwards compat with pre-init repos.)"""
    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    log_text = f"shaD{f}sha0{f}sara{f}s@x.io{f}1700000000{f}feat: x{f}body{f}{r}"

    async def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            return Completed(0, log_text, "")
        if cmd[:2] == ["git", "diff-tree"]:
            return Completed(0, "src/foo.py\n", "")
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    [art] = [a for a in out if a.kind == "commit"]
    assert art.area is None
    assert "areas" not in (art.signals or {})


@pytest.mark.asyncio
async def test_gather_revert_artifact_inherits_area(tmp_path):
    """Revert artifacts (the second emission) should carry the same area as
    their underlying commit so per-area pages can show 'recently reverted'."""
    af = tmp_path / ".agent-forge"
    af.mkdir()
    (af / "contexts.yaml").write_text(
        "areas:\n  payments:\n    paths: [\"src/payments/**\"]\n", encoding="utf-8",
    )

    f = git_history._FIELD_SEP
    r = git_history._REC_SEP
    log_text = (
        f"shaR{f}sha0{f}sara{f}s@x.io{f}1700000000{f}"
        f"Revert \"feat(payments): caching\"{f}This reverts commit deadbeef.{f}{r}"
    )

    async def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return Completed(0, "true\n", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return Completed(0, "refs/remotes/origin/main\n", "")
        if cmd[:2] == ["git", "log"]:
            return Completed(0, log_text, "")
        if cmd[:2] == ["git", "diff-tree"]:
            return Completed(0, "src/payments/cache.py\n", "")
        return Completed(0, "", "")

    with patch.object(git_history.shutil, "which", return_value="/usr/bin/git"), \
         patch.object(git_history, "run_subprocess", fake_run):
        g = git_history.GitHistoryGatherer()
        out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})

    revert = next(a for a in out if a.kind == "revert")
    assert revert.area == "payments"
