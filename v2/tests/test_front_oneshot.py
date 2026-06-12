"""front/oneshot + wiring: e2e through main() with the fake provider, the
--json run-record contract, and the permission-Ask path with a stub Asker."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.tools.workspace import RootedWorkspace
from forge.drive.executor import ToolExecutor
from forge.drive.session import SessionHandle
from forge.front import oneshot
from forge.front.render import Renderer
from forge.front.wiring import FAKE_RESPONSE, main
from forge.kernel.events import PermissionAsked, PermissionDecided, ToolFinished
from forge.kernel.types import (
    Effects,
    ModelOutput,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from forge.policy import StandardPolicy
from forge.ports.tool import ToolCtx
from forge.testing import FakeProvider, MemoryStore

# --- main() e2e with the fake provider --------------------------------------------


def run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> int:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FORGE_SESSIONS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["forge", *argv])
    return main()


def test_main_run_json_emits_run_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run_main(
        monkeypatch, tmp_path, ["run", "--provider", "fake", "-p", "hello", "--json"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    record = json.loads(captured.out)
    assert record["outcome"] == "ok"
    assert record["turns"] == 1
    assert record["usage"] == {
        "input_tokens": 24,
        "output_tokens": 8,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert record["cost"] is None  # fake provider reports no pricing
    assert record["error"] is None
    assert record["sid"]
    # With --json the human rendering goes to stderr, keeping stdout parseable.
    assert FAKE_RESPONSE in captured.err
    logs = list((tmp_path / "sessions").glob("*.jsonl"))
    assert len(logs) == 1 and logs[0].stem == record["sid"]


def test_main_run_plain_renders_to_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run_main(monkeypatch, tmp_path, ["run", "--provider", "fake", "-p", "hello"])
    captured = capsys.readouterr()
    assert rc == 0
    assert FAKE_RESPONSE in captured.out
    assert "ok" in captured.out  # the TurnFinished footer


def test_main_missing_credentials_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    rc = run_main(monkeypatch, tmp_path, ["run", "-p", "hello"])
    assert rc == 2
    assert "credentials" in capsys.readouterr().err


def test_main_model_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FORGE_MODEL", "fake-model-x")
    rc = run_main(
        monkeypatch, tmp_path, ["run", "--provider", "fake", "-p", "hi", "--json"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "ok"


# --- permission-Ask path -----------------------------------------------------------


class ExternalTool:
    """EXTERNAL effects make the standard guard chain return Ask."""

    spec = ToolSpec(
        name="notify",
        description="sends a notification beyond the workspace",
        params={"type": "object", "properties": {}},
        effects=Effects.EXTERNAL,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        return ToolResult("", "notified")


def ask_handle(
    tmp_path: Path, asker: oneshot.StaticAsker
) -> tuple[SessionHandle, MemoryStore]:
    tool = ExternalTool()
    ws = RootedWorkspace(tmp_path)
    policy = StandardPolicy(
        model="m", context_tokens=200_000, tools=(tool.spec,), ws_root=ws.root
    )
    provider = FakeProvider(
        [
            ModelOutput(blocks=(ToolCall("c1", "notify", {}),), usage=Usage(10, 5)),
            ModelOutput(blocks=(TextBlock("done"),), usage=Usage(10, 5)),
        ]
    )
    store = MemoryStore()
    handle = SessionHandle.open(
        store,
        provider=provider,
        executor=ToolExecutor((tool,), ws),
        policy=policy,
        asker=asker,
    )
    return handle, store


async def test_ask_path_allowed_runs_tool(tmp_path: Path) -> None:
    asker = oneshot.StaticAsker(allow=True)
    handle, store = ask_handle(tmp_path, asker)
    sink = io.StringIO()
    rc = await oneshot.run_once(
        handle,
        "go",
        renderer=Renderer(out=io.StringIO(), color=False),
        json_out=True,
        out=sink,
    )
    record = json.loads(sink.getvalue())
    assert rc == 0 and record["outcome"] == "ok" and record["turns"] == 2
    assert [q.tool for q in asker.questions] == ["notify"]
    bodies = [env.body for env in store.replay()]
    assert any(isinstance(b, PermissionAsked) for b in bodies)
    decided = [b for b in bodies if isinstance(b, PermissionDecided)]
    assert decided and decided[0].allowed and decided[0].source == "user"
    finished = [b for b in bodies if isinstance(b, ToolFinished)]
    assert finished[0].result.call_id == "c1"
    assert finished[0].result.content == "notified"
    assert not finished[0].result.is_error


async def test_ask_path_denied_blocks_tool(tmp_path: Path) -> None:
    asker = oneshot.StaticAsker(allow=False)
    handle, store = ask_handle(tmp_path, asker)
    sink = io.StringIO()
    rc = await oneshot.run_once(
        handle,
        "go",
        renderer=Renderer(out=io.StringIO(), color=False),
        json_out=True,
        out=sink,
    )
    record = json.loads(sink.getvalue())
    # The deny closes the call with an error result; the turn still ends "ok".
    assert rc == 0 and record["outcome"] == "ok"
    assert len(asker.questions) == 1
    bodies = [env.body for env in store.replay()]
    decided = [b for b in bodies if isinstance(b, PermissionDecided)]
    assert decided and not decided[0].allowed and decided[0].source == "user"
    finished = [b for b in bodies if isinstance(b, ToolFinished)]
    assert finished[0].result.is_error
    assert "denied" in finished[0].result.content


async def test_json_sink_carries_only_the_record(tmp_path: Path) -> None:
    asker = oneshot.StaticAsker(allow=True)
    handle, _ = ask_handle(tmp_path, asker)
    sink = io.StringIO()
    await oneshot.run_once(
        handle,
        "go",
        renderer=Renderer(out=io.StringIO(), color=False),
        json_out=True,
        out=sink,
    )
    lines = [line for line in sink.getvalue().splitlines() if line]
    assert len(lines) == 1
    json.loads(lines[0])
