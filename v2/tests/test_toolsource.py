"""ports/source + executor call-time resolution: swapping the source's tools
between two execute() calls changes what the executor sees — no frozen table."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from forge.drive.executor import UNKNOWN_EFFECTS, ToolExecutor
from forge.kernel.types import Effects, ToolCall, ToolResult, ToolSpec
from forge.ports.source import StaticToolSource, ToolSource
from forge.ports.tool import Tool, ToolCtx


class StubWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str) -> Path:
        return self._root / path


class NamedTool:
    def __init__(
        self, name: str, content: str, effects: Effects = Effects.READ_PATH
    ) -> None:
        self.spec = ToolSpec(
            name=name, description=f"tool {name}", params={"type": "object"}, effects=effects
        )
        self._content = content

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        return ToolResult("", self._content)


class SwappableSource:
    """Mutable ToolSource: replace() swaps the toolset and bumps generation."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._by_name: dict[str, Tool] = {t.spec.name: t for t in tools}
        self._generation = 0

    def replace(self, tools: Iterable[Tool]) -> None:
        self._by_name = {t.spec.name: t for t in tools}
        self._generation += 1

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._by_name.values())


def call(name: str, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, args={})


# --- StaticToolSource ------------------------------------------------------------


def test_static_source_lookup_and_all() -> None:
    a, b = NamedTool("a", "A"), NamedTool("b", "B")
    source = StaticToolSource((a, b))
    assert source.get("a") is a
    assert source.get("missing") is None
    assert source.all() == (a, b)
    assert source.generation == 0


def test_sources_satisfy_the_protocol_and_tuples_do_not() -> None:
    assert isinstance(StaticToolSource(()), ToolSource)
    assert isinstance(SwappableSource(), ToolSource)
    assert not isinstance((), ToolSource)


# --- executor back-compat ----------------------------------------------------------


async def test_executor_still_accepts_a_plain_iterable(tmp_path: Path) -> None:
    ex = ToolExecutor((NamedTool("a", "hello"),), StubWorkspace(tmp_path))
    result = await ex.execute(call("a"), asyncio.Event())
    assert not result.is_error and result.content == "hello"
    assert ex.effects_of("a") == Effects.READ_PATH


# --- call-time resolution -------------------------------------------------------------


async def test_swap_between_two_execute_calls_is_visible(tmp_path: Path) -> None:
    source = SwappableSource((NamedTool("a", "first"),))
    ex = ToolExecutor(source, StubWorkspace(tmp_path))

    result = await ex.execute(call("a"), asyncio.Event())
    assert not result.is_error and result.content == "first"

    source.replace(())  # the reconnect dropped the tool
    result = await ex.execute(call("a"), asyncio.Event())
    assert result.is_error and "unknown tool" in result.content

    source.replace((NamedTool("a", "second"),))  # ...and brought it back changed
    result = await ex.execute(call("a"), asyncio.Event())
    assert not result.is_error and result.content == "second"


async def test_effects_of_resolves_at_call_time(tmp_path: Path) -> None:
    source = SwappableSource((NamedTool("t", "x", effects=Effects.READ_PATH),))
    ex = ToolExecutor(source, StubWorkspace(tmp_path))
    assert ex.effects_of("t") == Effects.READ_PATH

    source.replace((NamedTool("t", "x", effects=Effects.EXEC),))
    assert ex.effects_of("t") == Effects.EXEC

    source.replace(())
    assert ex.effects_of("t") == UNKNOWN_EFFECTS


def test_generation_is_a_cheap_change_token() -> None:
    source = SwappableSource((NamedTool("a", "x"),))
    before = source.generation
    source.replace((NamedTool("b", "y"),))
    assert source.generation != before
