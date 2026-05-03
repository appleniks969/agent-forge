"""
_subprocess.py — async subprocess utility with abort-signal propagation.

Private leaf module (no internal imports). One seam for every subprocess call
in the codebase: BashTool, GrepTool's rg fallback, and every git/gh shell-out
in autonomous.py.

Why this exists
---------------
The old code used a mix of ``subprocess.run`` (blocking — freezes the event
loop, can't be cancelled) and ad-hoc ``asyncio.create_subprocess_*`` (no
abort-signal handling — Ctrl-C couldn't kill a hung Bash tool call). This
module collapses both patterns into one async helper that:

  * Races the child against an ``asyncio.Event`` abort signal — kills the
    child within milliseconds when the signal fires.
  * Honours a timeout (raises ``asyncio.TimeoutError`` and kills the child).
  * Optionally raises ``CalledProcessError`` on non-zero exit (``check=True``).
  * Always reaps the child in a finally block so we never leak zombies.

Returns
-------
A frozen ``Completed`` dataclass with ``returncode``, ``stdout``, ``stderr``,
and ``aborted`` — field-compatible with ``subprocess.CompletedProcess`` for
drop-in migration; ``aborted=True`` when the abort signal fired.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Completed:
    """Result of a subprocess run. Field-compatible with subprocess.CompletedProcess."""
    returncode: int
    stdout: str
    stderr: str
    aborted: bool = False


async def run(
    cmd: list[str] | str,
    *,
    cwd: str | None = None,
    timeout: float | None = 120.0,
    signal: asyncio.Event | None = None,
    shell: bool = False,
    check: bool = False,
    merge_stderr: bool = False,
) -> Completed:
    """
    Run a subprocess asynchronously, racing against ``signal`` and ``timeout``.

    Parameters
    ----------
    cmd            list[str] when shell=False (exec); str when shell=True.
    cwd            Working directory for the child process.
    timeout        Seconds before the child is killed and TimeoutError raised.
                   None disables the timeout.
    signal         asyncio.Event; .set() kills the child and returns
                   ``Completed(aborted=True)`` (no exception).
    shell          If True, use create_subprocess_shell; else create_subprocess_exec.
    check          If True, non-zero rc raises subprocess.CalledProcessError
                   (skipped when aborted).
    merge_stderr   If True, capture stderr into stdout (asyncio.subprocess.STDOUT)
                   and leave Completed.stderr empty.

    Behaviour
    ---------
    * Abort     — child killed; returns Completed(aborted=True). No exception.
    * Timeout   — child killed; raises asyncio.TimeoutError.
    * check=True with rc != 0 — raises subprocess.CalledProcessError.
    * Otherwise — returns Completed.

    Always waits for the child to actually exit before returning (no zombies).
    """
    stderr_dest = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    if shell:
        if not isinstance(cmd, str):
            raise TypeError("shell=True requires a str command")
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=stderr_dest, cwd=cwd,
        )
    else:
        if isinstance(cmd, str):
            raise TypeError("shell=False requires a list[str] command")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=stderr_dest, cwd=cwd,
        )

    aborted = False
    timed_out = False
    stdout_b: bytes = b""
    stderr_b: bytes = b""

    try:
        comm_task = asyncio.create_task(proc.communicate())
        sig_task: asyncio.Task[None] | None = None
        waiters: set[asyncio.Task] = {comm_task}
        if signal is not None:
            sig_task = asyncio.create_task(signal.wait())
            waiters.add(sig_task)

        done, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )

        if comm_task in done:
            stdout_b, stderr_b = comm_task.result()
            if sig_task is not None and not sig_task.done():
                sig_task.cancel()
        else:
            # Either the abort signal fired or we timed out (no completed task).
            if sig_task is not None and sig_task in done:
                aborted = True
            else:
                timed_out = True
                if sig_task is not None and not sig_task.done():
                    sig_task.cancel()
            comm_task.cancel()
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Best-effort drain of whatever output the child managed to produce.
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=2.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b = b"", b""
    finally:
        # Reap the child so we never leak a zombie, even on exception.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = "" if merge_stderr else stderr_b.decode("utf-8", errors="replace")

    if timed_out:
        raise asyncio.TimeoutError(f"Command timed out after {timeout}s")

    rc = proc.returncode if proc.returncode is not None else -1
    completed = Completed(returncode=rc, stdout=stdout, stderr=stderr, aborted=aborted)

    if check and rc != 0 and not aborted:
        raise subprocess.CalledProcessError(rc, cmd, output=stdout, stderr=stderr)

    return completed
