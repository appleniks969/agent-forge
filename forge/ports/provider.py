"""Provider port: one completion method plus model introspection.

Layer: ports — Protocols over kernel types only, no logic. Adapters raise
TransientProviderError for retryable faults and FatalProviderError otherwise;
drive/retry.py retries Transient only. Deltas go to on_delta for rendering
and never enter kernel state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from forge.kernel.types import Delta, ModelInfo, ModelOutput, ModelRequest


class TransientProviderError(Exception):
    """Retryable provider fault: rate limit, overload, transport hiccup."""


class FatalProviderError(Exception):
    """Non-retryable provider fault: auth, invalid request, context overflow."""


class Provider(Protocol):
    async def complete(
        self,
        req: ModelRequest,
        on_delta: Callable[[Delta], None] | None = None,
    ) -> ModelOutput: ...

    async def info(self, model: str) -> ModelInfo: ...
