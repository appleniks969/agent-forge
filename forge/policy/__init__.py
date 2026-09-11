"""policy: pure context, prompt, and guard policies plus their composition.

Layer: policy — pure and synchronous, imports kernel only. StandardPolicy
satisfies the kernel Policy Protocol by composing the three sibling modules;
import individual policies from their defining module (forge.policy.context,
.prompt, .guard) — one import path per name, no re-export aliases.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import PurePath

from forge.kernel.state import SessionState
from forge.kernel.types import (
    Effects,
    Effort,
    ModelRequest,
    Purpose,
    ToolCall,
    ToolSpec,
    Usage,
    UserMessage,
    Verdict,
)
from forge.policy import context as _context
from forge.policy import guard as _guard
from forge.policy import prompt as _prompt

COMPACTION_INSTRUCTION = (
    "Summarize this conversation so the summary can replace the transcript. "
    "Capture: the user's goal, decisions made and why, files touched (exact "
    "paths), the current state of the work, and what remains to be done. "
    "Be specific and concise; output only the summary."
)

ToolSupplier = Callable[[], Sequence[ToolSpec]]


class StandardPolicy:
    """The standard composition: prompt thunks (content-hash memoized), the
    recency+action-log context window, and the standard guard chain.

    Token estimation starts at the chars/4 heuristic; observe_usage() with
    the Usage from each completed ModelOutput calibrates it against reality
    (the driver feeds this back after every model call — spec 3.7).
    Tools may be a static tuple or a supplier (e.g. MCP reconnect
    changes the set mid-session); the supplier is re-queried at every build.
    """

    def __init__(
        self,
        *,
        model: str,
        context_tokens: int,
        tools: Sequence[ToolSpec] | ToolSupplier,
        ws_root: PurePath,
        sections: Iterable[_prompt.SectionThunk] = (),
        guards: Sequence[_guard.Guard] = _guard.STANDARD_GUARDS,
        budget: _context.ContextBudget | None = None,
        effort: Effort = Effort.MED,
        max_turns: int = 40,
        result_cap_bytes: int = 50 * 1024,
    ) -> None:
        self._model = model
        self._tools: ToolSupplier = (
            tools if callable(tools) else (lambda frozen=tuple(tools): frozen)
        )
        self._ws_root = ws_root
        self._chain = _guard.GuardChain(tuple(guards))
        self._budget = budget or _context.default_budget(context_tokens)
        self._effort = effort
        self._max_turns = max_turns
        self._result_cap_bytes = result_cap_bytes
        self._estimator = _context.TokenEstimator()
        self._last_estimate = 0
        self._assembler = _prompt.PromptAssembler(
            (
                _prompt.identity_section(),
                *sections,
                _prompt.tools_section(lambda: tuple(self._tools())),
            )
        )

    # --- kernel Policy Protocol ------------------------------------------------

    @property
    def result_cap_bytes(self) -> int:
        return self._result_cap_bytes

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def build_request(self, state: SessionState, purpose: Purpose) -> ModelRequest:
        window = _context.select_window(
            state.messages,
            summary=state.summary,
            budget=self._budget,
            estimator=self._estimator,
        )
        self._last_estimate = window.estimated_tokens
        system = self._assembler.build()
        if purpose == "compaction":
            return ModelRequest(
                model=self._model,
                system=system,
                messages=window.messages + (UserMessage(COMPACTION_INSTRUCTION),),
                tools=(),
                effort=Effort.LOW,
                purpose="compaction",
            )
        return ModelRequest(
            model=self._model,
            system=system,
            messages=window.messages,
            tools=tuple(self._tools()),
            effort=self._effort,
            purpose="turn",
        )

    def judge(self, call: ToolCall, effects: Effects) -> Verdict:
        if effects == Effects(0):
            effects = _guard.FULL_CAUTION
        return self._chain.judge(call, effects, self._ws_root)

    def should_compact(self, state: SessionState) -> bool:
        return _context.should_compact(
            state.messages,
            summary=state.summary,
            budget=self._budget,
            estimator=self._estimator,
        )

    # --- calibration -------------------------------------------------------------

    @property
    def estimator(self) -> _context.TokenEstimator:
        return self._estimator

    def observe_usage(self, usage: Usage) -> None:
        """Calibrate token estimation against the Usage of the ModelOutput that
        answered the most recently built request."""
        self._estimator = self._estimator.recalibrated(self._last_estimate, usage)
