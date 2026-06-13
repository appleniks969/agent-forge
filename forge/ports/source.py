"""ToolSource port: a live, name-addressable view over the available tools.

Layer: ports — Protocols over kernel types plus trivial impls only.
Consumers (the executor, the prompt's tools supplier) resolve through the
source AT CALL TIME, so a source whose contents change mid-session — an MCP
reconnect swapping server tools — is visible to the very next lookup.
generation is a cheap monotonic change token: an unchanged generation
guarantees an unchanged toolset, so consumers may skip re-reading all().
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from forge.ports.tool import Tool


@runtime_checkable
class ToolSource(Protocol):
    @property
    def generation(self) -> int: ...

    def get(self, name: str) -> Tool | None: ...

    def all(self) -> tuple[Tool, ...]: ...


class StaticToolSource:
    """A fixed toolset; generation never moves."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._by_name: dict[str, Tool] = {t.spec.name: t for t in tools}

    @property
    def generation(self) -> int:
        return 0

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._by_name.values())
