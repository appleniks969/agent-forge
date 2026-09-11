"""ToolExecutor: validation, path containment, output caps, sanitization.

Layer: drive — the async shell; imports kernel + ports. execute() never
raises (except task cancellation, which must propagate): unknown tools,
invalid arguments, workspace escapes, and tool exceptions all become error
ToolResults. Tools resolve through a ToolSource AT CALL TIME — there is no
frozen tool table, so an MCP reconnect that swaps server tools is visible
to the very next effects_of()/execute(). Containment is executor-level:
every schema property with format == "path" (including nested objects
and array items) is resolved through
the injected Workspace BEFORE run(), so forgetting containment is
impossible, including for third-party tools. Output truncation has exactly
one knob: output_cap_bytes. Error sanitization (class-name prefix, $HOME
redaction, no traceback) follows the legacy sanitize_exception contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from forge.kernel.types import Effects, ToolCall, ToolResult
from forge.ports.source import StaticToolSource, ToolSource
from forge.ports.tool import Tool, ToolCtx, Workspace, WorkspaceEscape

TRUNCATION_MARKER = "\n[output truncated]"
CANCELLED_CONTENT = "cancelled"
# Calls for tools we cannot see a spec for must serialize, never parallelize.
UNKNOWN_EFFECTS = Effects.WRITE_PATH | Effects.EXEC


def redact_home(text: str) -> str:
    """Replace the user's home directory with ~ (identity + path-existence leak)."""
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def sanitize_error(exc: BaseException) -> str:
    """LLM-safe error string: class-name prefix, $HOME redacted, no traceback."""
    text = str(exc)
    msg = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return redact_home(msg)


def _type_ok(expected: str, value: Any) -> bool:
    match expected:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "array":
            return isinstance(value, list | tuple)
        case "object":
            return isinstance(value, Mapping)
        case "null":
            return value is None
        case _:
            return True  # unknown schema types are not the executor's fight


def validate_args(schema: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    """Minimal JSON-schema check: required, top-level property types, enum."""
    errors: list[str] = []
    props: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in args:
            errors.append(f"missing required argument {name!r}")
    if schema.get("additionalProperties") is False:
        errors.extend(f"unexpected argument {name!r}" for name in args if name not in props)
    for name, value in args.items():
        prop = props.get(name)
        if not isinstance(prop, Mapping):
            continue
        expected = prop.get("type")
        if isinstance(expected, str) and not _type_ok(expected, value):
            errors.append(f"argument {name!r} must be {expected}, got {type(value).__name__}")
            continue
        if "enum" in prop and value not in prop["enum"]:
            errors.append(f"argument {name!r} must be one of {prop['enum']!r}")
    return errors


class ToolExecutor:
    def __init__(
        self,
        tools: Iterable[Tool] | ToolSource,
        ws: Workspace,
        *,
        output_cap_bytes: int = 48_000,
    ) -> None:
        # Back-compat: a plain iterable freezes into a StaticToolSource; a
        # ToolSource is consulted per call, never snapshotted.
        self._source: ToolSource = (
            tools if isinstance(tools, ToolSource) else StaticToolSource(tools)
        )
        self._ws = ws
        self._cap = output_cap_bytes

    def effects_of(self, name: str) -> Effects:
        tool = self._source.get(name)
        return tool.spec.effects if tool is not None else UNKNOWN_EFFECTS

    async def execute(self, call: ToolCall, cancel: asyncio.Event) -> ToolResult:
        tool = self._source.get(call.name)
        if tool is None:
            return ToolResult(call.id, f"unknown tool: {call.name}", is_error=True)
        if cancel.is_set():
            return ToolResult(call.id, CANCELLED_CONTENT, is_error=True)
        errors = validate_args(tool.spec.params, call.args)
        if errors:
            return ToolResult(call.id, "invalid arguments: " + "; ".join(errors), is_error=True)
        try:
            args = self._contain(tool.spec.params, call.args)
        except WorkspaceEscape as exc:
            return ToolResult(call.id, sanitize_error(exc), is_error=True)
        try:
            result = await tool.run(args, ToolCtx(ws=self._ws, cancel=cancel))
        except asyncio.CancelledError:
            raise  # batch abort must reach the TaskGroup
        except Exception as exc:  # noqa: BLE001 — last resort: tools must not raise
            return ToolResult(call.id, sanitize_error(exc), is_error=True)
        # The executor owns the call_id stamp: tools cannot mislabel results.
        return ToolResult(call.id, self._cap_output(result.content), is_error=result.is_error)

    def _contain_value(self, schema: Mapping[str, Any], value: Any) -> Any:
        if schema.get("format") == "path" and isinstance(value, str):
            return str(self._ws.resolve(value))
        if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
            item_schema = schema["items"]
            return [self._contain_value(item_schema, item) for item in value]
        if isinstance(value, Mapping) and (
            "properties" in schema or schema.get("type") == "object"
        ):
            return self._contain(schema, value)
        return value

    def _contain(self, schema: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(args)
        for name, prop in schema.get("properties", {}).items():
            if name in out and isinstance(prop, Mapping):
                out[name] = self._contain_value(prop, out[name])
        return out

    def _cap_output(self, content: str) -> str:
        raw = content.encode("utf-8", errors="replace")
        if len(raw) <= self._cap:
            return content
        return raw[: self._cap].decode("utf-8", errors="ignore") + TRUNCATION_MARKER
