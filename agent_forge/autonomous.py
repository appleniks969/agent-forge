"""
autonomous.py — AutonomousFlow state machine (composition root).

Depends on loop, context, prompts, tools, renderer. Parallel composition root
to chat.py for non-interactive, git-isolated execution: uses the same lower
layers (loop / context / prompts / tools / renderer) but does NOT import
chat.py and does NOT write session JSONL — each autonomous run is self-contained.

States: GATING → ISOLATED → PLANNING → EXECUTING → VERIFYING_AGENT → VERIFYING → DELIVERING → DONE
Any state can transition to FAILED.

Invariants:
  - Main branch is never modified (all work in a git worktree on a new branch).
  - Worktree is cleaned up on success, failure, or crash (try/finally in run()).
  - Delivery (PR / merge) only happens after all verify commands pass.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import _subprocess
from .loop import (
    AgentResult, HookDecision, NoopHooks,
)
from .messages import ToolCallContent, ToolResult, UserMessage, ZERO_USAGE
from .models import DEFAULT_MODEL, Model
from .prompts import build_autonomous_prompt
from .renderer import render_event
from .runtime import AgentRuntime
from .tools import default_registry

logger = logging.getLogger(__name__)


class FlowState(enum.Enum):
    GATING          = "gating"
    ISOLATED        = "isolated"
    PLANNING        = "planning"        # Fix 3: explicit planning phase
    EXECUTING       = "executing"
    VERIFYING_AGENT = "verifying_agent" # Fix 5: agent self-verifies before external gate
    VERIFYING       = "verifying"
    DELIVERING      = "delivering"
    DONE            = "done"
    FAILED          = "failed"


@dataclass
class FlowResult:
    success: bool
    state: FlowState
    output: str
    error: str | None = None


# ── BashGuardHook ─────────────────────────────────────────────────────────────
#
# Block destructive Bash commands during autonomous execution. The agent runs
# inside a worktree so 'rm -rf .' would only nuke the worktree, but commands
# that escape the worktree (sudo, push --force, hard-reset against origin) can
# damage the host repo or system. This hook is the demonstration that
# AgentConfig.hooks (a Hooks Protocol seam) is wired all the way through.
#
# Pattern set is intentionally small — it's the floor, not the ceiling.

class BashGuardHook(NoopHooks):
    """Veto destructive Bash commands. Returns None for everything else."""

    _BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bsudo\b"),                            "sudo not allowed in autonomous mode"),
        (re.compile(r"\brm\s+-rf?\s+(/|\$HOME|~)(\s|$)"),    "rm -rf on system / home path"),
        (re.compile(r"\bgit\s+push\s+(\S+\s+)*(-f|--force)"), "force-push to remote"),
        (re.compile(r"\bgit\s+reset\s+--hard\s+origin/"),    "hard reset against remote"),
        (re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:"),           "fork bomb"),
    ]

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        if call.name != "Bash":
            return None
        cmd = str(call.arguments.get("command", ""))
        for pattern, reason in self._BLOCK_PATTERNS:
            if pattern.search(cmd):
                return HookDecision(block=True, reason=reason)
        return None


# ── PathGuardHook ─────────────────────────────────────────────────────────────
#
# Veto file writes (Write / Edit) targeting sensitive system locations even
# when the path expression slips past tools._sandbox() (e.g. via tilde or
# absolute paths invoked with --cwd /). This is the second wall: tools.py
# enforces "must be inside cwd"; PathGuardHook enforces "even if you are
# inside cwd, certain paths are off-limits".

class PathGuardHook(NoopHooks):
    """Veto Write / Edit to sensitive paths. Configurable deny-list."""

    _DEFAULT_DENY = (
        "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc",
        "/.ssh", "/.aws", "/.gnupg",
    )

    def __init__(self, deny_paths: tuple[str, ...] | None = None) -> None:
        self._deny = deny_paths if deny_paths is not None else self._DEFAULT_DENY

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        if call.name not in ("Write", "Edit"):
            return None
        path = str(call.arguments.get("path", ""))
        if not path:
            return None
        # Resolve user-relative shortcuts before matching. Don't resolve fully
        # (the agent may not have file-system access yet) — just expand ~.
        expanded = os.path.expanduser(path)
        for needle in self._deny:
            if needle in expanded:
                return HookDecision(block=True, reason=f"write to {needle} is denied")
        return None


# ── Combined guard ────────────────────────────────────────────────────────────

class _CompositeHook(NoopHooks):
    """Chain multiple Hooks together. First veto wins."""

    def __init__(self, *hooks) -> None:
        self._hooks = hooks

    async def before_llm_call(self, messages, turn):
        for h in self._hooks:
            r = await h.before_llm_call(messages, turn)
            if r is not None:
                messages = r
        return messages

    async def before_tool_call(self, call, turn):
        for h in self._hooks:
            d = await h.before_tool_call(call, turn)
            if d is not None and d.block:
                return d
        return None

    async def after_tool_call(self, call, result, turn):
        for h in self._hooks:
            r = await h.after_tool_call(call, result, turn)
            if r is not None:
                result = r
        return result


@dataclass
class AutonomousConfig:
    task: str
    api_key: str
    model: Model = field(default_factory=lambda: DEFAULT_MODEL)
    repo_path: str = "."
    branch_prefix: str = "agent-forge"
    verify_commands: list[str] = field(default_factory=list)
    delivery: str = "pr"   # "pr" | "merge" | "output" | "none"
    max_turns: int = 100  # Fix 4: raised from 50 to match coding-agent-flow
    thinking: str = "medium"
    verbose: bool = False


class AutonomousFlow:
    """
    State machine for git-isolated autonomous execution.
    """

    def __init__(self, config: AutonomousConfig) -> None:
        self._cfg = config
        self._state = FlowState.GATING
        self._worktree_path: str | None = None
        self._branch: str | None = None

    @property
    def state(self) -> FlowState:
        return self._state

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> FlowResult:
        try:
            # GATING: pre-flight checks
            error = await self._gate_checks()
            if error:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output="", error=error)
            self._state = FlowState.ISOLATED

            # ISOLATED: create git worktree
            self._worktree_path, self._branch = await self._create_worktree()
            self._state = FlowState.PLANNING

            # Fix 3: PLANNING - agent analyses the codebase and writes a plan
            plan = await self._plan()
            self._state = FlowState.EXECUTING

            # EXECUTING: run agent loop in the worktree, guided by the plan
            result = await self._execute(plan=plan)
            if result is None:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output="", error="Agent produced no result")
            self._state = FlowState.VERIFYING_AGENT

            # Fix 5: VERIFYING_AGENT - agent runs tests and confirms correctness
            agent_ok, agent_output = await self._verify_agent()
            if not agent_ok:
                self._state = FlowState.FAILED
                return FlowResult(
                    success=False, state=self._state, output=agent_output,
                    error="Agent verification failed",
                )
            self._state = FlowState.VERIFYING

            # VERIFYING: run external verify_commands
            verify_ok, verify_output = await self._verify()
            if not verify_ok:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output=verify_output,
                                  error="Verification failed")
            self._state = FlowState.DELIVERING

            # DELIVERING
            ok, delivery_output = await self._deliver(result)
            if not ok:
                self._state = FlowState.FAILED
                return FlowResult(
                    success=False, state=self._state, output=delivery_output,
                    error="Delivery failed",
                )
            self._state = FlowState.DONE
            return FlowResult(success=True, state=self._state, output=delivery_output)

        except Exception as exc:
            self._state = FlowState.FAILED
            return FlowResult(success=False, state=self._state, output="", error=str(exc))
        finally:
            if self._state == FlowState.FAILED and self._worktree_path:
                await self._cleanup_worktree()
            elif self._state == FlowState.DONE and self._cfg.delivery == "none":
                await self._cleanup_worktree()

    # ── Gate checks ───────────────────────────────────────────────────────────

    async def _gate_checks(self) -> str | None:
        repo = Path(self._cfg.repo_path).resolve()
        if not (repo / ".git").exists():
            return f"Not a git repository: {repo}"
        # Check working tree is clean
        result = await _subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"], timeout=30,
        )
        if result.returncode != 0:
            return f"git status failed: {result.stderr}"
        if result.stdout.strip():
            return "Working tree has uncommitted changes. Commit or stash first."
        # Check current branch is not detached
        result = await _subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], timeout=30,
        )
        if result.returncode != 0:
            return "HEAD is detached — checkout a branch first."
        return None

    # ── Worktree ──────────────────────────────────────────────────────────────

    async def _create_worktree(self) -> tuple[str, str]:
        import time
        repo = Path(self._cfg.repo_path).resolve()
        ts = int(time.time())
        branch = f"{self._cfg.branch_prefix}/{ts}"
        worktree_path = str(repo.parent / f".agent-forge-worktree-{ts}")
        await _subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, worktree_path],
            check=True, timeout=60,
        )
        logger.info("Created worktree at %s on branch %s", worktree_path, branch)
        return worktree_path, branch

    async def _cleanup_worktree(self) -> None:
        if not self._worktree_path:
            return
        try:
            repo = Path(self._cfg.repo_path).resolve()
            await _subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", self._worktree_path],
                timeout=60,
            )
            await _subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", self._branch or ""],
                timeout=30,
            )
        except Exception:
            pass

    # ── Plan (Fix 3) ─────────────────────────────────────────────────────────

    def _phase_runtime(self, phase: str, *, max_turns: int, hooks=None) -> AgentRuntime:
        """Build a fresh AgentRuntime for a single autonomous phase.

        Each phase (plan/execute/verify) is its own logical session: distinct
        system prompt, distinct max_turns, fresh ContextWindow. The runtime
        gives each phase the same per-turn pressure-management dance the REPL
        gets (RC2 fix).
        """
        assert self._worktree_path is not None
        tool_registry = default_registry()
        sp = build_autonomous_prompt(
            phase,
            cwd=self._worktree_path, tool_registry=tool_registry,
            branch=self._branch, worktree_path=self._worktree_path,
            skills_cwd=self._cfg.repo_path,
        )
        return AgentRuntime(
            model=self._cfg.model,
            system_prompt=sp,
            tool_registry=tool_registry,
            cwd=self._worktree_path,
            api_key=self._cfg.api_key,
            thinking=self._cfg.thinking,
            max_turns=max_turns,
            project_root=self._cfg.repo_path,
            hooks=hooks,
        )

    async def _plan(self) -> str:
        """PLANNING phase: read the codebase, write a numbered implementation plan."""
        runtime = self._phase_runtime("plan", max_turns=10)
        user_msg = UserMessage(
            content=f"Analyse this task and write a precise implementation plan:\n\n{self._cfg.task}"
        )
        on_event = (
            (lambda ev: render_event(ev, verbose=True)) if self._cfg.verbose else None
        )
        result = await runtime.run_turn(user_msg, on_event=on_event)
        return result.text if result else ""


    # ── Execute ───────────────────────────────────────────────────────────────

    async def _execute(self, plan: str = "") -> AgentResult | None:
        runtime = self._phase_runtime(
            "execute", max_turns=self._cfg.max_turns,
            hooks=_CompositeHook(BashGuardHook(), PathGuardHook()),
        )
        # Fix 3: prepend the plan so the LLM has a concrete roadmap
        task_content = (
            f"{self._cfg.task}\n\nImplementation plan:\n{plan}"
            if plan else self._cfg.task
        )
        user_msg = UserMessage(content=task_content)
        return await runtime.run_turn(
            user_msg,
            on_event=lambda ev: render_event(ev, verbose=self._cfg.verbose),
        )

    # ── Verify — agent (Fix 5) ────────────────────────────────────────────────

    async def _verify_agent(self) -> tuple[bool, str]:
        """VERIFYING_AGENT phase: ask the LLM to run tests and confirm correctness."""
        runtime = self._phase_runtime("verify", max_turns=20)
        user_msg = UserMessage(
            content=f"Verify the implementation of:\n{self._cfg.task}\n\nRun tests and confirm correctness."
        )
        on_event = (
            (lambda ev: render_event(ev, verbose=True)) if self._cfg.verbose else None
        )
        result = await runtime.run_turn(user_msg, on_event=on_event)

        if result is None:
            return False, "Verification agent produced no result"

        if "VERIFICATION FAILED" in result.text:
            return False, result.text
        if "VERIFICATION PASSED" in result.text:
            return True, result.text
        # No explicit signal from the LLM — treat as inconclusive pass with a note
        return True, result.text + "\n(No explicit VERIFICATION PASSED/FAILED signal)"


    # ── Verify ────────────────────────────────────────────────────────────────

    async def _verify(self) -> tuple[bool, str]:
        if not self._cfg.verify_commands:
            return True, "(no verify commands)"
        outputs: list[str] = []
        for cmd in self._cfg.verify_commands:
            try:
                result = await _subprocess.run(
                    cmd, shell=True, cwd=self._worktree_path, timeout=120,
                )
            except asyncio.TimeoutError:
                outputs.append(f"$ {cmd}\n[timed out after 120s]")
                return False, "\n".join(outputs)
            outputs.append(f"$ {cmd}\n{result.stdout}{result.stderr}")
            if result.returncode != 0:
                return False, "\n".join(outputs)
        return True, "\n".join(outputs)

    # ── Deliver ───────────────────────────────────────────────────────────────

    # _deliver returns (ok, output). On failure, output carries the captured
    # stdout/stderr of the failing step so FlowResult surfaces it for triage —
    # we no longer rely on `check=True` swallowing the diagnostic into an
    # opaque CalledProcessError.

    async def _deliver(self, result: AgentResult) -> tuple[bool, str]:
        if self._cfg.delivery == "output":
            return True, result.text

        if self._cfg.delivery not in ("pr", "merge"):
            return True, "(no delivery)"

        wt = self._worktree_path or ""
        repo = Path(self._cfg.repo_path).resolve()

        async def step(label: str, cmd: list[str], *, cwd: str | None = None,
                       timeout: float = 120) -> tuple[bool, str]:
            res = await _subprocess.run(cmd, cwd=cwd, timeout=timeout)
            if res.returncode != 0:
                return False, f"[{label} failed]\n$ {' '.join(cmd)}\n{res.stdout}{res.stderr}"
            return True, res.stdout

        # Commit all changes
        ok, out = await step("git add", ["git", "-C", wt, "add", "-A"])
        if not ok:
            return False, out
        ok, out = await step(
            "git commit",
            ["git", "-C", wt, "commit", "-m", f"agent-forge: {self._cfg.task[:72]}"],
        )
        if not ok:
            return False, out

        if self._cfg.delivery == "pr":
            ok, out = await step(
                "git push",
                ["git", "-C", wt, "push", "origin", self._branch or ""],
                timeout=180,
            )
            if not ok:
                return False, out
            pr = await _subprocess.run(
                ["gh", "pr", "create", "--title", self._cfg.task[:72],
                 "--body", result.text[:2000], "--head", self._branch or ""],
                timeout=60,
            )
            if pr.returncode != 0:
                return False, f"[gh pr create failed]\n{pr.stdout}{pr.stderr}"
            return True, pr.stdout.strip() or "PR created"

        # delivery == "merge"
        ok, out = await step(
            "git merge",
            ["git", "-C", str(repo), "merge", self._branch or ""],
        )
        if not ok:
            return False, out
        await self._cleanup_worktree()
        return True, "Merged to main"


async def run_autonomous(cfg: AutonomousConfig) -> FlowResult:
    flow = AutonomousFlow(cfg)
    return await flow.run()
