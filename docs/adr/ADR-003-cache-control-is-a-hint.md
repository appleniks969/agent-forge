# ADR-003 — `cache_control` is an Advisory Hint

**Status:** accepted (Phase 5)
**Date:** 2026-05-02

## Context

`SystemPromptSection.cache_control: bool` looked like a behavioural promise:
"if you set this to `True`, the section will be cached." In reality:

- It is a hint to the *Anthropic* provider only — `_to_api_messages()` reads
  it and stamps `cache_control={"type":"ephemeral"}` on the corresponding API
  block.
- A different provider (OpenAI, Ollama, fake) sees the field but is free to
  ignore it.
- Even on Anthropic, the API may or may not honour the breakpoint depending
  on context size, prefix-stability, and the provider's own caching state.

Naming the field after the Anthropic-specific concept locked downstream
implementers into thinking about cache_control as a contract. It is not.

## Decision

- Keep `SystemPromptSection.cache_control: bool` as the field name — renaming
  it would invalidate every user's currently-cached prompts.
- Add a `hint_cache: bool` `@property` alias to make the advisory nature
  clear at call sites (new code: `section.hint_cache`; old code:
  `section.cache_control` keeps working).
- Document the advisory semantics in the `SystemPromptSection` docstring
  and in `AGENTS.md` under "Policies".
- A future provider adapter is free to read `hint_cache` and do nothing —
  that is conformant.

## Consequences

**Positive**
- New providers know they can ignore the hint without violating any
  contract.
- Prompt-cache policy stays in the provider, where the wire-format details
  live. `system_prompt.py` no longer pretends to know which sections will
  actually cache.

**Negative**
- Two ways to spell the same thing for a release. We accept this rather
  than break callers.

**Future**
- If we ever want a richer hint (TTL, breakpoint name, etc.), we extend
  `SystemPromptSection` with optional fields rather than replacing
  `cache_control`. Whatever we add is also advisory.
