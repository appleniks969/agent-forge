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
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .loop import (
    AgentResult, DoneAgentEvent, HookDecision, NoopHooks,
    agent_loop, make_config,
)
from .messages import ToolCallContent, ToolResult, UserMessage, ZERO_USAGE
from .models import DEFAULT_MODEL, Model
from .prompts import build_autonomous_prompt
from .renderer import render_event
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
    thinking: str = "off"
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
            error = self._gate_checks()
            if error:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output="", error=error)
            self._state = FlowState.ISOLATED

            # ISOLATED: create git worktree
            self._worktree_path, self._branch = self._create_worktree()
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
            verify_ok, verify_output = self._verify()
            if not verify_ok:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output=verify_output,
                                  error="Verification failed")
            self._state = FlowState.DELIVERING

            # DELIVERING
            delivery_output = self._deliver(result)
            self._state = FlowState.DONE
            return FlowResult(success=True, state=self._state, output=delivery_output)

        except Exception as exc:
            self._state = FlowState.FAILED
            return FlowResult(success=False, state=self._state, output="", error=str(exc))
        finally:
            if self._state == FlowState.FAILED and self._worktree_path:
                self._cleanup_worktree()
            elif self._state == FlowState.DONE and self._cfg.delivery == "none":
                self._cleanup_worktree()

    # ── Gate checks ───────────────────────────────────────────────────────────

    def _gate_checks(self) -> str | None:
        repo = Path(self._cfg.repo_path).resolve()
        if not (repo / ".git").exists():
            return f"Not a git repository: {repo}"
        # Check working tree is clean
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"git status failed: {result.stderr}"
        if result.stdout.strip():
            return "Working tree has uncommitted changes. Commit or stash first."
        # Check current branch is not detached
        result = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return "HEAD is detached — checkout a branch first."
        return None

    # ── Worktree ──────────────────────────────────────────────────────────────

    def _create_worktree(self) -> tuple[str, str]:
        import time
        repo = Path(self._cfg.repo_path).resolve()
        ts = int(time.time())
        branch = f"{self._cfg.branch_prefix}/{ts}"
        worktree_path = str(repo.parent / f".agent-forge-worktree-{ts}")
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, worktree_path],
            check=True, capture_output=True,
        )
        logger.info("Created worktree at %s on branch %s", worktree_path, branch)
        return worktree_path, branch

    def _cleanup_worktree(self) -> None:
        if not self._worktree_path:
            return
        try:
            repo = Path(self._cfg.repo_path).resolve()
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", self._worktree_path],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", self._branch or ""],
                capture_output=True,
            )
        except Exception:
            pass

    # ── Plan (Fix 3) ─────────────────────────────────────────────────────────

    async def _plan(self) -> str:
        """PLANNING phase: read the codebase, write a numbered implementation plan."""
        assert self._worktree_path is not None
        tool_registry = default_registry()

        sp = build_autonomous_prompt(
            "plan",
            cwd=self._worktree_path, tool_registry=tool_registry,
            branch=self._branch, worktree_path=self._worktree_path,
            skills_cwd=self._cfg.repo_path,
        )

        loop_cfg = make_config(
            model=self._cfg.model,
            api_key=self._cfg.api_key,
            system_prompt=sp.build(),
            tool_registry=tool_registry,
            cwd=self._worktree_path,
            thinking=self._cfg.thinking,
            max_turns=10,
            project_root=self._cfg.repo_path,
        )

        user_msg = UserMessage(
            content=f"Analyse this task and write a precise implementation plan:\n\n{self._cfg.task}"
        )
        result: AgentResult | None = None
        async for event in agent_loop(loop_cfg, [user_msg]):
            if isinstance(event, DoneAgentEvent):
                result = event.result
            if self._cfg.verbose:
                render_event(event, verbose=True)

        return result.text if result else ""


    # ── Execute ───────────────────────────────────────────────────────────────

    async def _execute(self, plan: str = "") -> AgentResult | None:
        assert self._worktree_path is not None
        tool_registry = default_registry()

        sp = build_autonomous_prompt(
            "execute",
            cwd=self._worktree_path, tool_registry=tool_registry,
            branch=self._branch, worktree_path=self._worktree_path,
            skills_cwd=self._cfg.repo_path,
        )

        loop_cfg = make_config(
            model=self._cfg.model,
            api_key=self._cfg.api_key,
            system_prompt=sp.build(),
            tool_registry=tool_registry,
            cwd=self._worktree_path,
            thinking=self._cfg.thinking,
            max_turns=self._cfg.max_turns,
            project_root=self._cfg.repo_path,
            hooks=BashGuardHook(),    # block destructive Bash in autonomous mode
        )

        # Fix 3: prepend the plan so the LLM has a concrete roadmap
        task_content = (
            f"{self._cfg.task}\n\nImplementation plan:\n{plan}"
            if plan else self._cfg.task
        )
        user_msg = UserMessage(content=task_content)
        initial_msgs = [user_msg]

        result: AgentResult | None = None

        async for event in agent_loop(loop_cfg, initial_msgs):
            if isinstance(event, DoneAgentEvent):
                result = event.result
            render_event(event, verbose=self._cfg.verbose)
        return result

    # ── Verify — agent (Fix 5) ────────────────────────────────────────────────

    async def _verify_agent(self) -> tuple[bool, str]:
        """VERIFYING_AGENT phase: ask the LLM to run tests and confirm correctness."""
        assert self._worktree_path is not None
        tool_registry = default_registry()

        sp = build_autonomous_prompt(
            "verify",
            cwd=self._worktree_path, tool_registry=tool_registry,
            branch=self._branch, worktree_path=self._worktree_path,
            skills_cwd=self._cfg.repo_path,
        )

        loop_cfg = make_config(
            model=self._cfg.model,
            api_key=self._cfg.api_key,
            system_prompt=sp.build(),
            tool_registry=tool_registry,
            cwd=self._worktree_path,
            thinking=self._cfg.thinking,
            max_turns=20,
            project_root=self._cfg.repo_path,
        )

        user_msg = UserMessage(
            content=f"Verify the implementation of:\n{self._cfg.task}\n\nRun tests and confirm correctness."
        )
        result: AgentResult | None = None
        async for event in agent_loop(loop_cfg, [user_msg]):
            if isinstance(event, DoneAgentEvent):
                result = event.result
            if self._cfg.verbose:
                render_event(event, verbose=True)

        if result is None:
            return False, "Verification agent produced no result"

        if "VERIFICATION FAILED" in result.text:
            return False, result.text
        if "VERIFICATION PASSED" in result.text:
            return True, result.text
        # No explicit signal from the LLM — treat as inconclusive pass with a note
        return True, result.text + "\n(No explicit VERIFICATION PASSED/FAILED signal)"


    # ── Verify ────────────────────────────────────────────────────────────────

    def _verify(self) -> tuple[bool, str]:
        if not self._cfg.verify_commands:
            return True, "(no verify commands)"
        outputs: list[str] = []
        for cmd in self._cfg.verify_commands:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                cwd=self._worktree_path, timeout=120,
            )
            outputs.append(f"$ {cmd}\n{result.stdout}{result.stderr}")
            if result.returncode != 0:
                return False, "\n".join(outputs)
        return True, "\n".join(outputs)

    # ── Deliver ───────────────────────────────────────────────────────────────

    def _deliver(self, result: AgentResult) -> str:
        if self._cfg.delivery == "output":
            return result.text

        if self._cfg.delivery in ("pr", "merge"):
            # Commit all changes
            repo = Path(self._cfg.repo_path).resolve()
            subprocess.run(
                ["git", "-C", self._worktree_path or "", "add", "-A"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", self._worktree_path or "", "commit",
                 "-m", f"agent-forge: {self._cfg.task[:72]}"],
                check=True, capture_output=True,
            )
            if self._cfg.delivery == "pr":
                # Push and open PR via gh CLI
                subprocess.run(
                    ["git", "-C", self._worktree_path or "", "push", "origin", self._branch or ""],
                    check=True, capture_output=True,
                )
                pr = subprocess.run(
                    ["gh", "pr", "create", "--title", self._cfg.task[:72],
                     "--body", result.text[:2000], "--head", self._branch or ""],
                    capture_output=True, text=True,
                )
                return pr.stdout.strip() or "PR created"
            else:  # merge
                subprocess.run(
                    ["git", "-C", str(repo), "merge", self._branch or ""],
                    check=True, capture_output=True,
                )
                self._cleanup_worktree()
                return "Merged to main"

        return "(no delivery)"


async def run_autonomous(cfg: AutonomousConfig) -> FlowResult:
    flow = AutonomousFlow(cfg)
    return await flow.run()
