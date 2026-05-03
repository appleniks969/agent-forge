# ADR-002 — Provider as Protocol, SDK as Optional Extra

**Status:** accepted (Phase 1)
**Date:** 2026-05-02

## Context

`agent_forge.loop` originally typed `AgentConfig.provider` as the concrete
class `AnthropicProvider`. This had three downstream effects:

1. **Hard import of the SDK at loop load time.** Importing `agent_forge.loop`
   transitively imported `anthropic`. A user installing the package without
   `anthropic` (e.g. for a custom provider) saw `ImportError` at the package
   boundary, not at point of use.
2. **Type-checker dishonesty.** Tests passed `FakeProvider` (a duck-typed
   stand-in). With `--strict` mypy this is a type error.
3. **Discouraged alternative providers.** A user wanting to plug in OpenAI
   would have to either subclass `AnthropicProvider` (wrong) or modify
   `loop.py` (worse).

## Decision

- `provider.py` defines an `LLMProvider` `typing.Protocol` with a single
  `stream()` method.
- `AgentConfig.provider: LLMProvider` (the Protocol, not the concrete class).
- `loop.py` does not import `anthropic_provider` at module load. The lazy
  default-construction in `make_config()` performs the import only when no
  `provider=` kwarg was supplied.
- `pyproject.toml` declares `anthropic` as an optional extra:
  `pip install agent-forge[anthropic]` (or installed by default since the
  project still ships with `anthropic` as a base dep — but the architecture
  no longer depends on it).
- `provider.py` no longer re-exports the concrete `AnthropicProvider` —
  it is a Protocol-only seam.

## Consequences

**Positive**
- A second provider is a one-file addition (`openai_provider.py` etc.) with
  zero changes to `loop.py`, `runtime.py`, or any test.
- `import agent_forge` succeeds without `anthropic` installed (the best-effort
  `try: from .anthropic_provider import AnthropicProvider` in `__init__.py`
  silently sets the symbol to `None` if the SDK is missing).
- Tests' `FakeProvider` is now a type-correct member of the `LLMProvider`
  family — `--strict` mypy is happy.

**Negative**
- One indirection: code that used to do
  `from agent_forge.loop import AnthropicProvider` must now import from
  `agent_forge.anthropic_provider` (or top-level `agent_forge`).

**Open**
- A second provider was deliberately not added in this work. Adding one is a
  separate project that this ADR makes possible, not mandatory.
