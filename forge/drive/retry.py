"""Backoff wrapper around the Provider port — THE single retry surface.

Layer: drive — the async shell; imports kernel + ports. Retries
TransientProviderError only; FatalProviderError and everything else
propagate untouched. Delays are exponential with equal jitter
(half fixed, half random) so synchronized clients fan out; rng and
sleep are injectable so tests are deterministic and instant. Each
scheduled retry is reported through on_retry as a RetryScheduled
event BEFORE sleeping, so the log shows the wait as it begins.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from forge.kernel.events import RetryScheduled
from forge.kernel.types import Delta, ModelInfo, ModelOutput, ModelRequest
from forge.ports.provider import Provider, TransientProviderError


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 4  # total complete() attempts, not retries
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0


class RetryingProvider:
    """A Provider whose complete() retries transient faults with backoff.

    Delay after failed attempt n is min(max_delay, base * 2**(n-1))
    scaled by (0.5 + 0.5 * rng()) — equal jitter, never below half the
    exponential step. info() passes through unretried: it is cheap,
    rare, and its callers handle their own failures.
    """

    def __init__(
        self,
        inner: Provider,
        config: RetryConfig = RetryConfig(),
        *,
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_retry: Callable[[RetryScheduled], None] | None = None,
    ) -> None:
        self._inner = inner
        self._config = config
        self._rng = rng
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._on_retry = on_retry

    async def complete(
        self,
        req: ModelRequest,
        on_delta: Callable[[Delta], None] | None = None,
    ) -> ModelOutput:
        cfg = self._config
        for attempt in range(1, cfg.max_attempts + 1):
            try:
                return await self._inner.complete(req, on_delta)
            except TransientProviderError as exc:
                if attempt >= cfg.max_attempts:
                    raise
                delay = min(cfg.max_delay_s, cfg.base_delay_s * 2 ** (attempt - 1))
                delay *= 0.5 + 0.5 * self._rng()
                if self._on_retry is not None:
                    text = str(exc)
                    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
                    self._on_retry(RetryScheduled(attempt=attempt, delay_s=delay, reason=reason))
                await self._sleep(delay)
        raise AssertionError("unreachable: loop returns or re-raises")

    async def info(self, model: str) -> ModelInfo:
        return await self._inner.info(model)
