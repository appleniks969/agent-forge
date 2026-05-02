"""Tests for autonomous.py — BashGuardHook policy, gate checks, verify command
runner, and the early-failure path of the FlowState machine.

We do NOT exercise the LLM-driven phases (_plan / _execute / _verify_agent)
here — those would require either a real provider or wiring FakeProvider
through make_config, which agent-forge supports but autonomous.py does not
yet expose. Coverage of the agent-driven phases is via integration tests
(out of scope for this commit).
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agent_forge.autonomous import (
    AutonomousConfig, AutonomousFlow, BashGuardHook, FlowResult, FlowState,
    run_autonomous,
)
from agent_forge.loop import HookDecision
from agent_forge.messages import ToolCallContent
from agent_forge.models import DEFAULT_MODEL


# ── Helpers ──────────────────────────────────────────────────────────────────


def _git_repo(path: Path, *, dirty: bool = False, detached: bool = False) -> Path:
    """Initialise a minimal git repo at `path`. Creates one initial commit."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "x@y"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True,
    )
    if dirty:
        (path / "dirty.txt").write_text("uncommitted")
    if detached:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "--detach", head], check=True,
        )
    return path


def _cfg(repo_path: str, **overrides) -> AutonomousConfig:
    return AutonomousConfig(
        task="do a thing", api_key="sk-test", model=DEFAULT_MODEL,
        repo_path=repo_path, **overrides,
    )


def _call(cmd: str) -> ToolCallContent:
    return ToolCallContent(id="t1", name="Bash", arguments={"command": cmd})


# ── BashGuardHook ────────────────────────────────────────────────────────────


class TestBashGuardHook:
    """Each blocked pattern + the allow path. Hook is the demonstration that
    the Hooks Protocol seam is wired through — losing any of these tests means
    the autonomous safety floor regressed."""

    @pytest.mark.asyncio
    async def test_blocks_sudo(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("sudo apt install foo"), turn=1)
        assert d is not None and d.block is True
        assert "sudo" in d.reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_rm_rf_root(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("rm -rf /"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_blocks_rm_rf_home(self):
        # The regex matches `rm -rf ~` (bare home) followed by whitespace/end.
        # NOTE: `rm -rf ~/subdir` is NOT blocked — the trailing `/` after `~`
        # falls outside `(\s|$)`. That's a known gap in the floor; this test
        # documents the current behaviour, not the desired one.
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("rm -rf ~"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_blocks_rm_rf_dollar_home(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("rm -rf $HOME"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_blocks_force_push(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("git push origin main --force"), turn=1)
        assert d is not None and d.block is True
        assert "force" in d.reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_force_push_short_flag(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("git push origin main -f"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_blocks_hard_reset_to_origin(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call("git reset --hard origin/main"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_blocks_fork_bomb(self):
        hook = BashGuardHook()
        d = await hook.before_tool_call(_call(":(){ :|:& };:"), turn=1)
        assert d is not None and d.block is True

    @pytest.mark.asyncio
    async def test_allows_safe_bash(self):
        hook = BashGuardHook()
        for safe_cmd in [
            "ls -la",
            "pytest -q",
            "git status",
            "git push origin feature-branch",  # no -f / --force
            "git reset --hard HEAD~1",         # not against origin/
            "rm -rf node_modules",             # not /, not ~, not $HOME
            "echo hello",
        ]:
            d = await hook.before_tool_call(_call(safe_cmd), turn=1)
            assert d is None, f"unexpectedly blocked: {safe_cmd!r}"

    @pytest.mark.asyncio
    async def test_ignores_non_bash_tools(self):
        hook = BashGuardHook()
        # Even if the args contain a banned word, non-Bash tools pass through.
        call = ToolCallContent(
            id="t1", name="Read",
            arguments={"path": "sudo-and-rm-rf.txt"},
        )
        d = await hook.before_tool_call(call, turn=1)
        assert d is None

    @pytest.mark.asyncio
    async def test_inherits_noop_for_other_hook_methods(self):
        """BashGuardHook subclasses NoopHooks — before_llm_call and
        after_tool_call should still no-op."""
        hook = BashGuardHook()
        from agent_forge.messages import ToolResult
        assert await hook.before_llm_call([], 1) is None
        assert await hook.after_tool_call(_call("ls"), ToolResult(content="ok"), 1) is None

    @pytest.mark.asyncio
    async def test_handles_missing_command_arg_gracefully(self):
        """If a malformed Bash call has no 'command' arg, the hook should
        return None (allow) rather than crash on str(None)."""
        hook = BashGuardHook()
        bad = ToolCallContent(id="t1", name="Bash", arguments={})
        d = await hook.before_tool_call(bad, turn=1)
        assert d is None


# ── _gate_checks ─────────────────────────────────────────────────────────────


class TestGateChecks:
    def test_non_git_directory_rejected(self, tmp_path):
        flow = AutonomousFlow(_cfg(str(tmp_path)))
        err = flow._gate_checks()
        assert err is not None
        assert "Not a git repository" in err

    def test_clean_repo_passes(self, tmp_path):
        repo = _git_repo(tmp_path / "r")
        flow = AutonomousFlow(_cfg(str(repo)))
        assert flow._gate_checks() is None

    def test_dirty_repo_rejected(self, tmp_path):
        repo = _git_repo(tmp_path / "r", dirty=True)
        flow = AutonomousFlow(_cfg(str(repo)))
        err = flow._gate_checks()
        assert err is not None
        assert "uncommitted changes" in err

    def test_detached_head_rejected(self, tmp_path):
        repo = _git_repo(tmp_path / "r", detached=True)
        flow = AutonomousFlow(_cfg(str(repo)))
        err = flow._gate_checks()
        assert err is not None
        assert "detached" in err.lower()


# ── _verify (external command runner) ────────────────────────────────────────


class TestVerify:
    def test_no_commands_passes(self, tmp_path):
        flow = AutonomousFlow(_cfg(str(tmp_path), verify_commands=[]))
        flow._worktree_path = str(tmp_path)
        ok, msg = flow._verify()
        assert ok is True
        assert "no verify" in msg

    def test_all_passing_commands(self, tmp_path):
        flow = AutonomousFlow(_cfg(
            str(tmp_path),
            verify_commands=["true", "echo hello"],
        ))
        flow._worktree_path = str(tmp_path)
        ok, msg = flow._verify()
        assert ok is True
        assert "hello" in msg
        assert "$ true" in msg
        assert "$ echo hello" in msg

    def test_fails_fast_on_first_nonzero(self, tmp_path):
        flow = AutonomousFlow(_cfg(
            str(tmp_path),
            verify_commands=["true", "false", "echo never_runs"],
        ))
        flow._worktree_path = str(tmp_path)
        ok, msg = flow._verify()
        assert ok is False
        # First two commands recorded, third never executed
        assert "$ true" in msg
        assert "$ false" in msg
        assert "never_runs" not in msg

    def test_runs_in_worktree_cwd(self, tmp_path):
        # Sentinel file in tmp_path; pwd should reveal it
        (tmp_path / "marker.txt").write_text("x")
        flow = AutonomousFlow(_cfg(
            str(tmp_path), verify_commands=["ls marker.txt"],
        ))
        flow._worktree_path = str(tmp_path)
        ok, msg = flow._verify()
        assert ok is True
        assert "marker.txt" in msg


# ── run() — early-failure path: gate fails, no worktree created ──────────────


class TestRunGateFailurePath:
    @pytest.mark.asyncio
    async def test_gate_failure_returns_failed_state_no_worktree(self, tmp_path):
        # Non-git directory → gate fails before isolation
        flow = AutonomousFlow(_cfg(str(tmp_path)))
        result = await flow.run()
        assert isinstance(result, FlowResult)
        assert result.success is False
        assert result.state == FlowState.FAILED
        assert "Not a git repository" in (result.error or "")
        # Worktree was never created — verify nothing leaked next to tmp_path
        siblings = list(tmp_path.parent.glob(".agent-forge-worktree-*"))
        assert siblings == []

    @pytest.mark.asyncio
    async def test_run_autonomous_wraps_flow(self, tmp_path):
        result = await run_autonomous(_cfg(str(tmp_path)))
        assert isinstance(result, FlowResult)
        assert result.success is False  # not a git repo


# ── FlowState / FlowResult sanity ────────────────────────────────────────────


def test_flow_state_has_all_expected_states():
    expected = {
        "GATING", "ISOLATED", "PLANNING", "EXECUTING",
        "VERIFYING_AGENT", "VERIFYING", "DELIVERING",
        "DONE", "FAILED",
    }
    assert {s.name for s in FlowState} == expected


def test_flow_result_default_error_none():
    fr = FlowResult(success=True, state=FlowState.DONE, output="x")
    assert fr.error is None


def test_initial_state_is_gating():
    flow = AutonomousFlow(_cfg("."))
    assert flow.state == FlowState.GATING
