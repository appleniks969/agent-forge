"""
hidden_tests.py — evaluator-owned test suite for the TokenBucket task.

Run via pytest INSIDE the agent's workdir; imports `rate_limiter.TokenBucket`
written by the agent. Tests assert only what the task spec literally requires:

    __init__(self, rate: float, capacity: int)
    acquire(self, tokens: int = 1, blocking: bool = True,
            timeout: float | None = None) -> bool
    tokens : property  → current (continuously refilled) token count, float
    Thread-safe under concurrent calls.

Defensive choices:
  - Initial token count is NOT specified by the task. Most rate-limiter
    implementations start full; we test "starts at capacity" because every
    in-the-wild bucket spec implies it, but we tolerate ±0.5 token slack
    for impls that count from time of construction.
  - Timing tests use generous margins (≥ 100 ms slack) to avoid flakes on
    busy machines.
  - Concurrency test uses non-blocking acquires only — easier to assert
    correctness without timing entanglement.

Run:  pytest test_hidden.py -q
"""
from __future__ import annotations

import threading
import time

import pytest

from rate_limiter import TokenBucket  # type: ignore[import-not-found]


# ── Construction & basic acquire ───────────────────────────────────────────
def test_hidden_construct_with_rate_and_capacity():
    tb = TokenBucket(rate=10.0, capacity=10)
    assert tb is not None


def test_hidden_initial_tokens_at_capacity():
    """Spec is implicit but every reasonable impl starts the bucket full."""
    tb = TokenBucket(rate=10.0, capacity=10)
    assert tb.tokens >= 9.5  # ≥ capacity (allow tiny float slack)


def test_hidden_basic_acquire_returns_true():
    tb = TokenBucket(rate=10.0, capacity=10)
    assert tb.acquire() is True or tb.acquire() == True  # noqa: E712 (allow bool-ish)


def test_hidden_acquire_decreases_token_count():
    # Use a very slow rate so refill during the test is negligible.
    # (rate=0 is rejected by some defensive impls.)
    tb = TokenBucket(rate=0.001, capacity=10)
    before = tb.tokens
    tb.acquire(1)
    after = tb.tokens
    assert after <= before - 0.9  # ~1 token consumed


def test_hidden_tokens_property_is_numeric():
    tb = TokenBucket(rate=10.0, capacity=10)
    val = tb.tokens
    assert isinstance(val, (int, float))


# ── Multi-token acquire ────────────────────────────────────────────────────
def test_hidden_acquire_n_tokens():
    tb = TokenBucket(rate=0.001, capacity=10)
    r = tb.acquire(5)
    assert r is True or r == 1  # noqa: E712
    # After acquire(5), drain remaining 5 by single acquires.
    while tb.acquire(1, blocking=False):
        pass
    r2 = tb.acquire(1, blocking=False)
    assert r2 is False or r2 == 0  # noqa: E712


def test_hidden_acquire_more_than_available_non_blocking_fails():
    tb = TokenBucket(rate=0.001, capacity=10)
    # Drain to 3 tokens
    tb.acquire(7)
    assert tb.tokens <= 3.5
    # Asking for 5 should fail in non-blocking mode (only 3 available)
    result = tb.acquire(5, blocking=False)
    assert result is False or result == 0  # noqa: E712


# ── Capacity cap ───────────────────────────────────────────────────────────
def test_hidden_capacity_cap_holds_after_idle_wait():
    """Bucket cannot exceed capacity even after long idle period."""
    tb = TokenBucket(rate=100.0, capacity=5)  # would refill 100/s but cap=5
    time.sleep(0.2)  # 0.2s × 100/s = 20 tokens of refill, but cap=5
    assert tb.tokens <= 5.0 + 0.5  # tiny slack for float ops


# ── Non-blocking semantics ─────────────────────────────────────────────────
def test_hidden_non_blocking_returns_immediately_when_empty():
    tb = TokenBucket(rate=0.01, capacity=10)
    # Drain
    while tb.acquire(1, blocking=False):
        pass
    t0 = time.monotonic()
    result = tb.acquire(1, blocking=False)
    elapsed = time.monotonic() - t0
    assert result is False or result == 0  # noqa: E712
    assert elapsed < 0.1, f"non-blocking should not wait, took {elapsed:.3f}s"


# ── Blocking semantics ─────────────────────────────────────────────────────
def test_hidden_blocking_succeeds_after_refill():
    tb = TokenBucket(rate=10.0, capacity=10)
    # Drain
    while tb.acquire(1, blocking=False):
        pass
    # Now blocking acquire should succeed within reasonable time
    t0 = time.monotonic()
    result = tb.acquire(1, blocking=True, timeout=2.0)
    elapsed = time.monotonic() - t0
    assert result is True or result == 1  # noqa: E712
    assert elapsed < 2.0, f"should succeed before timeout, took {elapsed:.3f}s"


def test_hidden_blocking_timeout_returns_false_when_starved():
    tb = TokenBucket(rate=0.01, capacity=10)  # very slow refill
    # Drain
    while tb.acquire(1, blocking=False):
        pass
    t0 = time.monotonic()
    result = tb.acquire(1, blocking=True, timeout=0.2)
    elapsed = time.monotonic() - t0
    assert result is False or result == 0  # noqa: E712
    # Timeout should be honored: 0.2s ± 0.5s grace
    assert 0.15 <= elapsed <= 0.7, f"timeout dishonored, took {elapsed:.3f}s"


# ── Thread safety ──────────────────────────────────────────────────────────
def test_hidden_concurrent_non_blocking_no_overgrant():
    """50 threads, capacity=10, very slow refill → exactly 10 succeed."""
    tb = TokenBucket(rate=0.001, capacity=10)
    grants = []
    grants_lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()  # all threads start ~simultaneously
        ok = tb.acquire(1, blocking=False)
        with grants_lock:
            grants.append(bool(ok))

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    granted = sum(1 for g in grants if g)
    assert granted == 10, f"expected exactly 10 grants, got {granted}"
