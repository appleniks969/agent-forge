"""
autonomous.py — AutonomousFlow state machine (composition root).

Depends on loop, context, prompts, tools, renderer. Parallel composition root
to chat.py for non-interactive, git-isolated execution: uses the same lower
layers (loop / context / prompts / tools / renderer) but does NOT import
chat.py and does NOT write session JSONL — each autonomous run is self-contained.

States: GATING → ISOLATED → EXECUTING → VERIFYING → DELIVERING → DONE
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
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .context import ContextWindow, SectionName, SystemPrompt
from .loop import AgentResult, DoneAgentEvent, ThinkingDeltaAgentEvent, agent_loop, make_config
from .prompts import _TOOLS_SECTION
from .provider import DEFAULT_MODEL, Model, UserMessage, ZERO_USAGE
from .renderer import dim, render_event
from .tools import default_registry

logger = logging.getLogger(__name__)


class FlowState(enum.Enum):
    GATING    = "gating"
    ISOLATED  = "isolated"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DELIVERING = "delivering"
    DONE      = "done"
    FAILED    = "failed"


@dataclass
class FlowResult:
    success: bool
    state: FlowState
    output: str
    error: str | None = None


@dataclass
class AutonomousConfig:
    task: str
    api_key: str
    model: Model = field(default_factory=lambda: DEFAULT_MODEL)
    repo_path: str = "."
    branch_prefix: str = "agent-forge"
    verify_commands: list[str] = field(default_factory=list)
    delivery: str = "pr"   # "pr" | "merge" | "output" | "none"
    max_turns: int = 50
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
            self._state = FlowState.EXECUTING

            # EXECUTING: run agent loop in the worktree
            result = await self._execute()
            if result is None:
                self._state = FlowState.FAILED
                return FlowResult(success=False, state=self._state, output="", error="Agent produced no result")
            self._state = FlowState.VERIFYING

            # VERIFYING: run verify commands
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

    # ── Execute ───────────────────────────────────────────────────────────────

    async def _execute(self) -> AgentResult | None:
        assert self._worktree_path is not None
        tool_registry = default_registry()

        sp = SystemPrompt()
        sp.register(SectionName.IDENTITY, lambda: (
            "You are agent-forge running in autonomous mode inside an isolated git worktree.\n"
            "Your job is to complete the assigned task fully and correctly, then stop.\n"
            "There is no human in the loop between turns. Do not ask for confirmation.\n"
            "Do not commit, push, or open a pull request — the delivery system handles that after you finish."
        ))
        sp.register(SectionName.TOOLS, lambda: _TOOLS_SECTION)
        sp.register(SectionName.GUIDELINES, lambda: (
            "Guidelines:\n"
            "- Read a file before editing it. Use Edit (not Write) for existing files.\n"
            "- After making changes, run the available tests (pytest, npm test, cargo test, go test, etc.)"
            " and fix every failure before considering the task complete. If no tests exist, exercise the"
            " changed code path with a small script you delete afterwards.\n"
            "- Use the minimum number of tool calls. Use Grep and Find before Bash for search.\n"
            "- Use assert statements (not print) in any test or example you write.\n"
            "- Never add TODO comments, placeholder values, or stub implementations."
            " The task is to finish the work, not mark where it would go.\n"
            "- All tool paths are relative to the working directory.\n"
            "- When a tool returns is_error=true, diagnose before retrying. When output is truncated,"
            " refine the call with offset / limit / glob — do not re-run the same call.\n"
            "- If you hit an unrecoverable blocker (missing dependency, ambiguous spec, contradiction with"
            " existing code), describe the blocker clearly and stop. Do not invent a workaround.\n"
            "- End your final reply with a single section titled \"Changes made:\" listing every file you"
            " modified and a one-line reason for each. This becomes the PR/commit body."
        ))
        sp.register(SectionName.ENVIRONMENT, lambda: (
            f"Working directory: {self._worktree_path}\n"
            f"Branch: {self._branch}\n"
            f"Date: {__import__('datetime').date.today()}\n"
            "All file paths are relative to the working directory."
        ))

        loop_cfg = make_config(
            model=self._cfg.model,
            api_key=self._cfg.api_key,
            system_prompt=sp.build(),
            tool_registry=tool_registry,
            cwd=self._worktree_path,
            thinking=self._cfg.thinking,
            max_turns=self._cfg.max_turns,
        )

        user_msg = UserMessage(content=self._cfg.task)
        initial_msgs = [user_msg]

        result: AgentResult | None = None
        thinking_buf: list[str] = []

        def _flush_thinking() -> None:
            if not thinking_buf:
                return
            text = "".join(thinking_buf).strip()
            thinking_buf.clear()
            if not text:
                return
            preview = text.replace("\n", " ")
            if len(preview) > 300:
                preview = preview[:297] + "…"
            print(f"\n  {dim('💭 ' + preview)}", end="", flush=True)

        async for event in agent_loop(loop_cfg, initial_msgs):
            if isinstance(event, ThinkingDeltaAgentEvent):
                thinking_buf.append(event.delta)
            elif isinstance(event, DoneAgentEvent):
                _flush_thinking()
                result = event.result
            else:
                _flush_thinking()
                render_event(event, verbose=self._cfg.verbose)
        return result

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
