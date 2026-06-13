"""BashTool: cwd anchoring, error mapping, timeout, and process-group kill on cancel."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from pathlib import Path

import pytest

from forge.adapters.tools.bash import BashTool
from forge.adapters.tools.workspace import RootedWorkspace
from forge.ports.tool import ToolCtx
from forge.testing.conformance import check_tool_honors_cancel


@pytest.fixture
def ctx(tmp_path):
    return ToolCtx(ws=RootedWorkspace(tmp_path), cancel=asyncio.Event())


async def test_echo_succeeds(ctx):
    res = await BashTool().run({"command": "echo hello"}, ctx)
    assert not res.is_error
    assert res.content.strip() == "hello"


async def test_cwd_is_workspace_root(ctx):
    res = await BashTool().run({"command": "pwd"}, ctx)
    assert not res.is_error
    assert Path(res.content.strip()).resolve() == ctx.ws.root


async def test_nonzero_exit_is_error_with_exit_code(ctx):
    res = await BashTool().run({"command": "exit 3"}, ctx)
    assert res.is_error
    assert "3" in res.content


async def test_stderr_merged_into_stdout(ctx):
    res = await BashTool().run({"command": "echo oops 1>&2"}, ctx)
    assert not res.is_error
    assert "oops" in res.content


async def test_missing_command_is_error(ctx):
    res = await BashTool().run({}, ctx)
    assert res.is_error


async def test_timeout_kills_and_reports(ctx):
    start = time.monotonic()
    res = await BashTool().run({"command": "sleep 5", "timeout": 1}, ctx)
    assert res.is_error
    assert "timed out" in res.content
    assert time.monotonic() - start < 4


async def test_precancelled_returns_aborted_quickly(ctx):
    ctx.cancel.set()
    start = time.monotonic()
    res = await check_tool_honors_cancel(BashTool(), {"command": "sleep 5"}, ctx)
    assert res.is_error
    assert "abort" in res.content.lower()
    assert time.monotonic() - start < 3


async def test_cancel_kills_whole_process_group(ctx, tmp_path):
    # The backgrounded subshell's sleep would survive a direct-child-only
    # kill; killpg must take down the entire group.
    cmd = "echo $$ > pid.txt; (sleep 30; echo never) & sleep 30"
    task = asyncio.create_task(BashTool().run({"command": cmd}, ctx))
    pidfile = tmp_path / "pid.txt"
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("pid file never appeared")
    await asyncio.sleep(0.1)  # give the background subshell time to spawn

    ctx.cancel.set()
    res = await asyncio.wait_for(task, timeout=5)
    assert res.is_error
    assert "abort" in res.content.lower()

    # start_new_session makes the shell the group leader: pgid == $$.
    pgid = int(pidfile.read_text().strip())
    for _ in range(40):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break  # every group member is gone
        await asyncio.sleep(0.05)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        pytest.fail("process group survived cancel")
