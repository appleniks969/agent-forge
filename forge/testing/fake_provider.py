"""FakeProvider: a scripted Provider for tests.

Layer: testing — imports ports + kernel only. Pops one scripted item per
complete() call; Exception instances in the script are raised (use
TransientProviderError to exercise drive/retry.py). Records every request
and drives on_delta from the scripted output's blocks.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence

from forge.kernel.types import (
    Delta,
    Effort,
    ModelInfo,
    ModelOutput,
    ModelRequest,
    TextBlock,
    ThinkingBlock,
)
from forge.ports.provider import FatalProviderError


class FakeProvider:
    def __init__(
        self,
        script: Sequence[ModelOutput | Exception],
        *,
        model_info: ModelInfo | None = None,
    ) -> None:
        self._script: deque[ModelOutput | Exception] = deque(script)
        self._info = model_info
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        req: ModelRequest,
        on_delta: Callable[[Delta], None] | None = None,
    ) -> ModelOutput:
        self.requests.append(req)
        if not self._script:
            raise FatalProviderError("FakeProvider script exhausted")
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        if on_delta is not None:
            for block in item.blocks:
                if isinstance(block, TextBlock):
                    on_delta(Delta("text", block.text))
                elif isinstance(block, ThinkingBlock):
                    on_delta(Delta("thinking", block.text))
        return item

    async def info(self, model: str) -> ModelInfo:
        if self._info is not None:
            return self._info
        return ModelInfo(
            id=model,
            context_tokens=200_000,
            pricing=None,
            efforts=frozenset(Effort),
        )
