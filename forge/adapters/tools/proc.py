"""Async subprocess seam with process-group kill.

Layer: adapters/tools — asyncio is allowed here. The one place v2 spawns
subprocesses: start_new_session=True makes every child its own process-group
leader, and cancel/timeout/task-cancellation kill the whole group with
killpg, so shell pipelines and backgrounded children are reaped — not just
the direct child. POSIX only, like the rest of the sandbox story.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str
    aborted: bool = False


def _kill_group(pid: int) -> None:
    # start_new_session=True guarantees pgid == pid of the spawned child.
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run(
    cmd: list[str] | str,
    *,
    cwd: str | None = None,
    timeout: float | None = 120.0,
    cancel: asyncio.Event | None = None,
    shell: bool = False,
    merge_stderr: bool = False,
) -> Completed:
    """Run a subprocess, racing the cancel event and the timeout.

    Normal exits (any returncode) return Completed and never raise.
    Cancel set  -> group killed, returns Completed(aborted=True).
    Timeout     -> group killed, raises TimeoutError.
    Task cancel -> group killed, CancelledError propagates.
    """
    stderr_dest = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    if shell:
        if not isinstance(cmd, str):
            raise TypeError("shell=True requires a str command")
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_dest,
            cwd=cwd,
            start_new_session=True,
        )
    else:
        if isinstance(cmd, str):
            raise TypeError("shell=False requires a list[str] command")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_dest,
            cwd=cwd,
            start_new_session=True,
        )

    aborted = False
    timed_out = False
    stdout_b: bytes = b""
    stderr_b: bytes | None = b""
    comm = asyncio.ensure_future(proc.communicate())
    waiter = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
    waiters: set[asyncio.Future] = {comm} if waiter is None else {comm, waiter}
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if comm in done:
            stdout_b, stderr_b = comm.result()
        else:
            aborted = waiter is not None and waiter in done
            timed_out = not aborted
            _kill_group(proc.pid)
            # The kill closes the pipes, so communicate() returns the partial
            # output the child managed to produce; bound it just in case.
            try:
                stdout_b, stderr_b = await asyncio.wait_for(comm, timeout=2.0)
            except Exception:  # noqa: BLE001 — best-effort drain only
                stdout_b, stderr_b = b"", b""
    except asyncio.CancelledError:
        _kill_group(proc.pid)
        raise
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
        if not comm.done():
            comm.cancel()
        # Reap so we never leak a zombie, even on exception paths.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = "" if merge_stderr else (stderr_b or b"").decode("utf-8", errors="replace")
    if timed_out:
        raise TimeoutError(f"command timed out after {timeout}s")
    rc = proc.returncode if proc.returncode is not None else -1
    return Completed(returncode=rc, stdout=stdout, stderr=stderr, aborted=aborted)
