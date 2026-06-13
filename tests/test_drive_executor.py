"""drive/executor.py: validation, containment, caps, sanitization — never raises."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from forge.drive.executor import (
    CANCELLED_CONTENT,
    TRUNCATION_MARKER,
    UNKNOWN_EFFECTS,
    ToolExecutor,
    redact_home,
    sanitize_error,
    validate_args,
)
from forge.kernel.types import Effects, ToolCall, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx, WorkspaceEscape


class StubWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()
        if resolved != self._root and not resolved.is_relative_to(self._root):
            raise WorkspaceEscape(str(resolved))
        return resolved


class EchoTool:
    """Returns its received args (post-containment view) as the result content."""

    spec = ToolSpec(
        name="echo",
        description="echo args",
        params={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer"},
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        effects=Effects.READ_PATH,
    )

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        self.seen.append(args)
        return ToolResult("WRONG-ID", repr(dict(sorted(args.items()))))


class PathTool:
    spec = ToolSpec(
        name="read",
        description="read a path",
        params={
            "type": "object",
            "properties": {"path": {"type": "string", "format": "path"}},
            "required": ["path"],
        },
        effects=Effects.READ_PATH,
    )

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        self.seen.append(args)
        return ToolResult("", f"read {args['path']}")


class RaisingTool:
    spec = ToolSpec(name="boom", description="raises", params={"type": "object"})

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        raise self._exc


def executor(tmp_path: Path, *tools, cap: int = 48_000) -> ToolExecutor:
    return ToolExecutor(tools, StubWorkspace(tmp_path), output_cap_bytes=cap)


def call(name: str, args: dict[str, Any], cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, args=args)


async def test_happy_path_stamps_call_id(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    result = await ex.execute(call("echo", {"text": "hi"}), asyncio.Event())
    assert not result.is_error
    assert result.call_id == "c1"  # executor owns the stamp; tool said WRONG-ID
    assert "hi" in result.content


async def test_unknown_tool_is_error_result(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    result = await ex.execute(call("nope", {}), asyncio.Event())
    assert result.is_error and "unknown tool" in result.content


async def test_missing_required_argument(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    result = await ex.execute(call("echo", {}), asyncio.Event())
    assert result.is_error and "missing required argument 'text'" in result.content


async def test_wrong_type_and_enum_rejected(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    result = await ex.execute(call("echo", {"text": 7}), asyncio.Event())
    assert result.is_error and "'text' must be string" in result.content
    result = await ex.execute(call("echo", {"text": "x", "count": True}), asyncio.Event())
    assert result.is_error and "'count' must be integer" in result.content
    result = await ex.execute(call("echo", {"text": "x", "mode": "warp"}), asyncio.Event())
    assert result.is_error and "'mode' must be one of" in result.content


async def test_additional_properties_false_rejects_extras(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    result = await ex.execute(call("echo", {"text": "x", "bogus": 1}), asyncio.Event())
    assert result.is_error and "unexpected argument 'bogus'" in result.content


async def test_validation_failure_never_reaches_tool(tmp_path: Path) -> None:
    tool = EchoTool()
    ex = executor(tmp_path, tool)
    await ex.execute(call("echo", {}), asyncio.Event())
    assert tool.seen == []


async def test_path_field_resolved_through_workspace(tmp_path: Path) -> None:
    tool = PathTool()
    ex = executor(tmp_path, tool)
    result = await ex.execute(call("read", {"path": "sub/a.txt"}), asyncio.Event())
    assert not result.is_error
    assert tool.seen[0]["path"] == str((tmp_path / "sub/a.txt").resolve())


async def test_escape_is_error_result_not_exception(tmp_path: Path) -> None:
    tool = PathTool()
    ex = executor(tmp_path, tool)
    result = await ex.execute(call("read", {"path": "../../etc/passwd"}), asyncio.Event())
    assert result.is_error
    assert result.content.startswith("WorkspaceEscape")
    assert tool.seen == []  # containment happens BEFORE run()


async def test_absolute_path_outside_root_contained(tmp_path: Path) -> None:
    ex = executor(tmp_path, PathTool())
    result = await ex.execute(call("read", {"path": "/etc/passwd"}), asyncio.Event())
    assert result.is_error and "WorkspaceEscape" in result.content


async def test_output_capped_one_knob(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool(), cap=16)
    result = await ex.execute(call("echo", {"text": "y" * 100}), asyncio.Event())
    assert result.content.endswith(TRUNCATION_MARKER)
    assert len(result.content.encode()) <= 16 + len(TRUNCATION_MARKER.encode())


async def test_raising_tool_sanitized_with_class_prefix(tmp_path: Path) -> None:
    home = str(Path.home())
    ex = executor(tmp_path, RaisingTool(ValueError(f"no file {home}/secret.txt")))
    result = await ex.execute(call("boom", {}), asyncio.Event())
    assert result.is_error
    assert result.content == "ValueError: no file ~/secret.txt"


async def test_messageless_exception_keeps_class_name(tmp_path: Path) -> None:
    ex = executor(tmp_path, RaisingTool(RuntimeError()))
    result = await ex.execute(call("boom", {}), asyncio.Event())
    assert result.content == "RuntimeError"


async def test_preset_cancel_short_circuits(tmp_path: Path) -> None:
    tool = EchoTool()
    ex = executor(tmp_path, tool)
    cancel = asyncio.Event()
    cancel.set()
    result = await ex.execute(call("echo", {"text": "hi"}), cancel)
    assert result.is_error and result.content == CANCELLED_CONTENT
    assert tool.seen == []


async def test_effects_of_unknown_serializes(tmp_path: Path) -> None:
    ex = executor(tmp_path, EchoTool())
    assert ex.effects_of("echo") == Effects.READ_PATH
    assert ex.effects_of("mystery") == UNKNOWN_EFFECTS


def test_sanitize_and_redact_helpers() -> None:
    home = str(Path.home())
    assert sanitize_error(KeyError("k")) == "KeyError: 'k'"
    assert home not in redact_home(f"{home}/x and {home}/y")


def test_validate_args_empty_schema_accepts_anything() -> None:
    assert validate_args({}, {"whatever": object()}) == []
