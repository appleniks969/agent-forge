"""BashTool: run shell commands rooted at the workspace.

Layer: adapters/tools. Effects declare the honest worst case
(EXEC|READ_PATH|WRITE_PATH|NETWORK) so the guard chain and scheduler treat
shell as the hole it is; containment here is cwd=ws.root plus process-group
kill, not a sandbox claim. Never raises — errors are ToolResults with
call_id="" for the executor to stamp.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.adapters.tools import proc
from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx

_DEFAULT_TIMEOUT = 120.0


def _err(msg: str) -> ToolResult:
    return ToolResult(call_id="", content=f"Error: {msg}", is_error=True)


class BashTool:
    spec = ToolSpec(
        name="Bash",
        description=(
            "Execute a shell command in the workspace root. Use for running "
            "tests, builds, git operations, or any shell task. Chain "
            "dependent setup-then-run steps with && in a single call. Avoid "
            "interactive commands. Default timeout: 120s."
        ),
        params={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120)",
                    "default": 120,
                },
            },
            "required": ["command"],
        },
        effects=Effects.EXEC | Effects.READ_PATH | Effects.WRITE_PATH | Effects.NETWORK,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        command = str(args.get("command") or "")
        if not command.strip():
            return _err("no command provided")
        try:
            timeout = max(1.0, float(args.get("timeout") or _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            return _err("timeout must be a number")
        try:
            done = await proc.run(
                command,
                shell=True,
                cwd=str(ctx.ws.root),
                timeout=timeout,
                cancel=ctx.cancel,
                merge_stderr=True,
            )
        except TimeoutError:
            return _err(f"command timed out after {timeout:g}s")
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")
        if done.aborted:
            return _err("command aborted")
        output = done.stdout
        if done.returncode != 0 and not output.strip():
            output = f"exit code {done.returncode}"
        return ToolResult(call_id="", content=output, is_error=done.returncode != 0)
