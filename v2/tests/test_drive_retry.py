"""drive/retry.py: the single retry surface around the Provider port."""

from __future__ import annotations

import pytest

from forge.drive.retry import RetryConfig, RetryingProvider
from forge.kernel.events import RetryScheduled
from forge.kernel.types import Effort, ModelOutput, ModelRequest, TextBlock, Usage
from forge.ports.provider import FatalProviderError, TransientProviderError
from forge.testing import FakeProvider

OUT = ModelOutput(blocks=(TextBlock("done"),), usage=Usage(10, 5))


def req() -> ModelRequest:
    return ModelRequest(
        model="fake-model", system=(), messages=(), tools=(), effort=Effort.NONE, purpose="turn"
    )


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.retries: list[RetryScheduled] = []

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)

    def on_retry(self, event: RetryScheduled) -> None:
        self.retries.append(event)


def wrap(script: list, rec: Recorder, *, rng: float = 1.0, **cfg) -> tuple[RetryingProvider, FakeProvider]:
    inner = FakeProvider(script)
    provider = RetryingProvider(
        inner,
        RetryConfig(**cfg) if cfg else RetryConfig(),
        rng=lambda: rng,
        sleep=rec.sleep,
        on_retry=rec.on_retry,
    )
    return provider, inner


async def test_transient_retried_until_success() -> None:
    rec = Recorder()
    provider, inner = wrap(
        [TransientProviderError("rate limited"), TransientProviderError("overloaded"), OUT], rec
    )
    assert await provider.complete(req()) is OUT
    assert len(inner.requests) == 3
    # exponential: 0.5, 1.0 with rng=1.0 (full equal-jitter factor)
    assert rec.sleeps == [0.5, 1.0]
    assert [e.attempt for e in rec.retries] == [1, 2]
    assert [e.delay_s for e in rec.retries] == rec.sleeps
    assert rec.retries[0].reason == "TransientProviderError: rate limited"


async def test_retry_scheduled_emitted_before_sleep() -> None:
    order: list[str] = []
    rec = Recorder()

    async def sleep(delay: float) -> None:
        order.append("sleep")

    provider = RetryingProvider(
        FakeProvider([TransientProviderError("x"), OUT]),
        rng=lambda: 1.0,
        sleep=sleep,
        on_retry=lambda e: order.append("retry"),
    )
    await provider.complete(req())
    assert order == ["retry", "sleep"]


async def test_fatal_not_retried() -> None:
    rec = Recorder()
    provider, inner = wrap([FatalProviderError("bad auth"), OUT], rec)
    with pytest.raises(FatalProviderError):
        await provider.complete(req())
    assert len(inner.requests) == 1
    assert rec.sleeps == []
    assert rec.retries == []


async def test_other_exceptions_not_retried() -> None:
    rec = Recorder()
    provider, _ = wrap([ValueError("adapter bug"), OUT], rec)
    with pytest.raises(ValueError):
        await provider.complete(req())
    assert rec.sleeps == []


async def test_exhaustion_reraises_transient() -> None:
    rec = Recorder()
    errors = [TransientProviderError(f"e{i}") for i in range(3)]
    provider, inner = wrap(errors, rec, max_attempts=3)
    with pytest.raises(TransientProviderError, match="e2"):
        await provider.complete(req())
    assert len(inner.requests) == 3
    assert len(rec.retries) == 2  # no RetryScheduled for the final, fatal attempt


async def test_jitter_floor_is_half_the_exponential_step() -> None:
    rec = Recorder()
    provider, _ = wrap([TransientProviderError("x"), OUT], rec, rng=0.0)
    await provider.complete(req())
    assert rec.sleeps == [0.25]  # 0.5 * (0.5 + 0.5*0.0)


async def test_delay_capped_at_max() -> None:
    rec = Recorder()
    script = [TransientProviderError("a"), TransientProviderError("b"), OUT]
    provider, _ = wrap(script, rec, max_attempts=4, base_delay_s=10.0, max_delay_s=12.0)
    await provider.complete(req())
    assert rec.sleeps == [10.0, 12.0]  # second raw delay 20.0 hits the cap


async def test_on_delta_passed_through() -> None:
    rec = Recorder()
    provider, _ = wrap([TransientProviderError("x"), OUT], rec)
    deltas: list[str] = []
    await provider.complete(req(), on_delta=lambda d: deltas.append(d.text))
    assert deltas == ["done"]


async def test_info_passes_through_unretried() -> None:
    rec = Recorder()
    provider, _ = wrap([OUT], rec)
    info = await provider.info("fake-model")
    assert info.id == "fake-model"
    assert rec.sleeps == []
