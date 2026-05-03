"""
models.py — Model catalog: pricing + context-window descriptors.

Zero internal dependencies. Pure data: Model id → context_window, max_tokens,
reasoning capability, and ModelCost (per-million-token pricing for input,
output, cache_read, cache_write). Anything that needs to know a model's
properties imports from here, not provider.py.

Owns: ModelCost, Model (with .from_id classmethod), MODELS dict, DEFAULT_MODEL.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCost:
    """Per-million-token pricing for one model. All fields are USD."""
    input: float          # $ per 1M tokens
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class Model:
    """A single model's static descriptor: id, capacity, capability, cost.

    ``context_window`` is the maximum prompt + completion in tokens.
    ``max_tokens`` is the per-response output cap.
    ``reasoning`` indicates whether the model accepts a ``thinking`` parameter.
    """

    id: str
    context_window: int
    max_tokens: int
    reasoning: bool
    cost: ModelCost

    @classmethod
    def from_id(cls, model_id: str) -> "Model":
        """Return the ``Model`` whose ``id`` matches; raise ``ValueError`` if unknown."""
        if model_id not in MODELS:
            raise ValueError(f"Unknown model: {model_id!r}. Known: {list(MODELS)}")
        return MODELS[model_id]


_S46 = ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)
_S45 = ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)
_O47 = ModelCost(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25)
_H45 = ModelCost(input=1.0, output=5.0,  cache_read=0.10, cache_write=1.25)

MODELS: dict[str, Model] = {
    "claude-sonnet-4-6": Model("claude-sonnet-4-6", 1_000_000, 64_000,  True,  _S46),
    "claude-sonnet-4-5": Model("claude-sonnet-4-5",   200_000, 64_000,  True,  _S45),
    "claude-haiku-4-5":  Model("claude-haiku-4-5",    200_000, 64_000,  False, _H45),
    "claude-opus-4-7":   Model("claude-opus-4-7",   1_000_000, 128_000, True,  _O47),
}
DEFAULT_MODEL = MODELS["claude-sonnet-4-6"]
