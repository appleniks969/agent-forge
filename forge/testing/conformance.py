"""Conformance kits: what any Provider or Tool must satisfy.

Layer: testing — imports ports + kernel only. These checks define adapter #2
before it exists; a second provider or a third-party tool passes them or it
does not ship. Each function raises AssertionError with a precise message on
contract violation and returns the checked value for further assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.kernel.types import ModelOutput, ModelRequest, ToolResult, ToolSpec
from forge.ports.provider import Provider
from forge.ports.tool import Tool, ToolCtx


async def check_provider_contract(provider: Provider, req: ModelRequest) -> ModelOutput:
    """Provider returns a complete ModelOutput: whole tool calls plus usage."""
    out = await provider.complete(req)
    if not isinstance(out, ModelOutput):
        raise AssertionError(f"complete() returned {type(out).__name__}, not ModelOutput")
    for call in out.tool_calls:
        if not call.id:
            raise AssertionError("tool call missing id")
        if not call.name:
            raise AssertionError(f"tool call {call.id!r} missing name")
        if not isinstance(call.args, Mapping):
            raise AssertionError(f"tool call {call.id!r} args is not a mapping")
    usage = out.usage
    for label, value in (
        ("input_tokens", usage.input_tokens),
        ("output_tokens", usage.output_tokens),
        ("cache_read_tokens", usage.cache_read_tokens),
        ("cache_write_tokens", usage.cache_write_tokens),
    ):
        if value < 0:
            raise AssertionError(f"usage.{label} is negative: {value}")
    return out


async def check_tool_contract(tool: Tool, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
    """Tool exposes a well-formed spec and never raises — errors are ToolResults."""
    spec = tool.spec
    if not isinstance(spec, ToolSpec):
        raise AssertionError(f"tool.spec is {type(spec).__name__}, not ToolSpec")
    if not spec.name:
        raise AssertionError("tool spec has empty name")
    try:
        result = await tool.run(args, ctx)
    except Exception as exc:  # noqa: BLE001 — the contract under test is "never raises"
        raise AssertionError(
            f"tool {spec.name!r} raised {type(exc).__name__}: {exc}; "
            "tools must return an error ToolResult instead"
        ) from exc
    if not isinstance(result, ToolResult):
        raise AssertionError(
            f"tool {spec.name!r} returned {type(result).__name__}, not ToolResult"
        )
    return result


async def check_tool_honors_cancel(
    tool: Tool, args: Mapping[str, Any], ctx: ToolCtx
) -> ToolResult:
    """With ctx.cancel already set, the tool still returns (never raises)."""
    if not ctx.cancel.is_set():
        raise AssertionError("pass a ToolCtx whose cancel event is already set")
    return await check_tool_contract(tool, args, ctx)
