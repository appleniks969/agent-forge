"""drive/bus.py: bounded per-subscriber queues, persist-before-delivery."""

from __future__ import annotations

import asyncio

import pytest

from forge.drive.bus import Bus
from forge.kernel.events import Envelope, TextDelta, UserSubmitted, make_envelope
from forge.testing import MemoryStore


def durable_env(seq: int, text: str = "hi") -> Envelope:
    return make_envelope(seq=seq, sid="s1", body=UserSubmitted(text), ts=float(seq))


def transient_env(seq: int, text: str = "d") -> Envelope:
    return make_envelope(seq=seq, sid="s1", body=TextDelta(text), ts=float(seq))


async def drain(sub, n: int) -> list[Envelope]:
    out: list[Envelope] = []
    async for env in sub:
        out.append(env)
        if len(out) == n:
            break
    return out


async def test_durable_persisted_before_any_delivery() -> None:
    store = MemoryStore()
    bus = Bus(store)
    sub = bus.subscribe()
    env = durable_env(0)
    bus.publish(env)
    # publish() is synchronous: the store already has the envelope even though
    # no subscriber task has run yet — persist-before-run.
    assert store.replay() == (env,)
    assert (await drain(sub, 1)) == [env]


async def test_transient_is_bus_only() -> None:
    store = MemoryStore()
    bus = Bus(store)
    sub = bus.subscribe()
    env = transient_env(0)
    bus.publish(env)
    assert store.replay() == ()
    assert (await drain(sub, 1)) == [env]


async def test_every_subscriber_gets_every_envelope() -> None:
    bus = Bus(MemoryStore())
    a, b = bus.subscribe(), bus.subscribe()
    envs = [durable_env(0), transient_env(1), durable_env(2)]
    for env in envs:
        bus.publish(env)
    assert (await drain(a, 3)) == envs
    assert (await drain(b, 3)) == envs


async def test_slow_subscriber_drops_oldest_not_stalls() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe(maxsize=2)
    envs = [durable_env(i) for i in range(5)]
    for env in envs:
        bus.publish(env)  # never awaits, never blocks
    bus.close()
    received = [env async for env in sub]
    # drop-oldest: seqs 0-2 evicted by publishes, seq 3 evicted to seat the
    # close sentinel; the newest envelope always survives.
    assert received == [envs[4]]
    assert sub.dropped == 4


async def test_drop_counts_zero_for_keeping_up_subscriber() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe(maxsize=2)
    for i in range(10):
        bus.publish(durable_env(i))
        assert (await drain(sub, 1))[0].seq == i
    assert sub.dropped == 0


async def test_close_ends_iteration_after_draining() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe()
    bus.publish(durable_env(0))
    bus.close()
    received = [env async for env in sub]
    assert [e.seq for e in received] == [0]


async def test_publish_after_close_raises() -> None:
    bus = Bus(MemoryStore())
    bus.close()
    with pytest.raises(RuntimeError):
        bus.publish(durable_env(0))
    with pytest.raises(RuntimeError):
        bus.subscribe()


async def test_subscription_close_detaches() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe()
    bus.publish(durable_env(0))
    sub.close()
    bus.publish(durable_env(1))  # after detach: not delivered, no error
    received = [env async for env in sub]
    assert [e.seq for e in received] == [0]


async def test_blocked_consumer_woken_by_publish() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe()

    async def consume() -> Envelope:
        async for env in sub:
            return env
        raise AssertionError("ended without delivery")

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # park the consumer on the empty queue
    bus.publish(durable_env(7))
    assert (await asyncio.wait_for(task, 1)).seq == 7


async def test_wait_empty_returns_after_drain() -> None:
    bus = Bus(MemoryStore())
    sub = bus.subscribe()
    bus.publish(durable_env(0))
    bus.publish(durable_env(1))
    drained: list[int] = []

    async def consume() -> None:
        async for env in sub:
            drained.append(env.seq)
            if len(drained) == 2:
                break

    task = asyncio.create_task(consume())
    await bus.wait_empty()
    await task
    assert drained == [0, 1]
