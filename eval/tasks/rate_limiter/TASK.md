# Task

In the current working directory, create two files:

1. `rate_limiter.py` — implement a thread-safe `TokenBucket` class:
   - `__init__(self, rate: float, capacity: int)` — `rate` is tokens added per second; `capacity` is the max bucket size.
   - `acquire(self, tokens: int = 1, blocking: bool = True, timeout: float | None = None) -> bool` — try to consume `tokens`. If `blocking`, wait up to `timeout` seconds (None = forever) and return True when granted, False on timeout. If non-blocking, return immediately (True if granted, False otherwise).
   - `tokens` property returning the current (continuously refilled) token count as a float.
   - Must be safe under concurrent calls from multiple threads.

2. `test_rate_limiter.py` — pytest tests covering:
   - basic single acquire,
   - capacity cap (bucket cannot exceed capacity),
   - non-blocking failure when empty,
   - blocking acquire with a timeout that succeeds after refill,
   - blocking acquire with a timeout that fails,
   - concurrent acquires from multiple threads do not over-grant.

Use `/tmp/harness-eval/venv/bin/pytest -q` to run the tests; iterate until all pass. Do not edit anything outside this directory.
