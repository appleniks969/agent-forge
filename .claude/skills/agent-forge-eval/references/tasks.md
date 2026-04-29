# Eval Task Prompts

Six tasks graded in two tiers. Copy the prompt text verbatim into the CLI command.

---

## Simple Tasks
*(1 tool call or less expected)*

### S1 — Python palindrome
```
Write a Python function to check if a string is a palindrome
```

### S2 — TypeScript Queue
```
Write a TypeScript Queue data structure class with enqueue, dequeue, peek, and isEmpty methods
```

### S3 — JavaScript debounce
```
Write a JavaScript debounce function that delays invoking a function until after N milliseconds have elapsed
```

---

## Complex Tasks
*(multiple turns, file creation expected)*

### C1 — LRU Cache
```
Implement a TypeScript LRU (Least Recently Used) cache class with get, put, and clear methods. Use a Map for O(1) lookups and a doubly-linked list for O(1) eviction. Include full TypeScript generics, JSDoc comments, and at least 5 unit tests using a simple assert pattern (no test framework needed).
```
Expected files: `lru-cache.ts`, `lru-cache.test.ts`

### C2 — Token Bucket Rate Limiter
```
Implement a TypeScript token bucket rate limiter class with: configurable rate (tokens/sec) and burst capacity, consume(tokens) method that returns true/false, getAvailableTokens() method, and reset(). Include JSDoc and 5 unit tests with a simple assert pattern.
```
Expected files: `tokenBucketRateLimiter.ts`, `tokenBucketRateLimiter.test.ts`

### C3 — Generic EventEmitter
```
Implement a TypeScript generic EventEmitter class with on<T>(event, listener), off<T>(event, listener), once<T>(event, listener), and emit<T>(event, data) methods. Use TypeScript mapped types or a generic EventMap type parameter so events are fully type-safe. Include JSDoc and 5 unit tests with a simple assert pattern.
```
Expected files: `EventEmitter.ts`, `EventEmitter.test.ts`
