"""Shared test helpers: a scriptable Policy and a synchronous mini-driver.

The mini-driver executes kernel Effects the way drive/ will: CallModel pops a
scripted output, RunTools yields ToolOutcomes, AskUser yields scripted
PermissionAnswers. A pluggable `pick` callback chooses which pending input is
fed next, which is how the hypothesis test randomizes interleavings.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from forge.kernel.events import Event, make_envelope
from forge.kernel.state import SessionState
from forge.kernel.step import (
    AskUser,
    CallModel,
    Finish,
    Input,
    ModelResponded,
    PermissionAnswer,
    RunTools,
    ToolOutcome,
    UserInput,
    step,
)
from forge.kernel.types import (
    Allow,
    Ask,
    AssistantMessage,
    Block,
    Deny,
    Effects,
    Effort,
    ModelOutput,
    ModelRequest,
    PermissionQuestion,
    PromptSection,
    Purpose,
    Stability,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    TurnResult,
    Usage,
    Verdict,
)

READ_TOOL = ToolSpec(
    name="read",
    description="read a file",
    params={"type": "object", "properties": {"path": {"type": "string", "format": "path"}}},
    effects=Effects.READ_PATH,
)
BASH_TOOL = ToolSpec(name="bash", description="run a command", effects=Effects.EXEC)
DEFAULT_TOOLS = (READ_TOOL, BASH_TOOL)


class SimPolicy:
    """Policy whose verdicts and compaction trigger are scripted per test."""

    def __init__(
        self,
        *,
        max_turns: int = 8,
        result_cap_bytes: int = 4096,
        verdicts: dict[str, str] | None = None,  # call_id -> allow|deny|ask
        compact_when: Callable[[SessionState], bool] | None = None,
        tools: tuple[ToolSpec, ...] = DEFAULT_TOOLS,
    ) -> None:
        self._max_turns = max_turns
        self._cap = result_cap_bytes
        self.verdicts = dict(verdicts or {})
        self.compact_when = compact_when
        self.tools = tools
        self.judged: list[tuple[ToolCall, Effects]] = []

    @property
    def result_cap_bytes(self) -> int:
        return self._cap

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def build_request(self, state: SessionState, purpose: Purpose) -> ModelRequest:
        return ModelRequest(
            model="fake-model",
            system=(PromptSection("identity", "you are forge", Stability.STATIC),),
            messages=state.messages,
            tools=self.tools,
            effort=Effort.NONE,
            purpose=purpose,
        )

    def judge(self, call: ToolCall, effects: Effects) -> Verdict:
        self.judged.append((call, effects))
        match self.verdicts.get(call.id, "allow"):
            case "deny":
                return Deny("blocked by test policy")
            case "ask":
                return Ask(PermissionQuestion(call.id, call.name, f"allow {call.name}?"))
            case _:
                return Allow()

    def should_compact(self, state: SessionState) -> bool:
        return bool(self.compact_when and self.compact_when(state))


@dataclass
class Script:
    outputs: deque[ModelOutput]
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    answers: dict[str, bool] = field(default_factory=dict)

    def result_for(self, call: ToolCall) -> ToolResult:
        return self.tool_results.get(call.id, ToolResult(call.id, f"result of {call.id}"))


@dataclass
class SimRun:
    state: SessionState
    events: list[Event]
    finishes: list[TurnResult]


def simulate(
    policy: SimPolicy,
    script: Script,
    *,
    user_texts: tuple[str, ...] = ("do the thing",),
    pick: Callable[[int], int] | None = None,
) -> SimRun:
    state = SessionState()
    events: list[Event] = []
    finishes: list[TurnResult] = []
    for text in user_texts:
        pending: list[Input] = [UserInput(text)]
        guard = 0
        while pending:
            guard += 1
            assert guard < 1000, "mini-driver did not converge"
            idx = pick(len(pending)) if pick else 0
            inp = pending.pop(idx)
            result = step(state, inp, policy)
            state = result.state
            events.extend(result.events)
            for eff in result.effects:
                if isinstance(eff, CallModel):
                    out = script.outputs.popleft()
                    pending.append(ModelResponded(output=out, request=eff.request))
                elif isinstance(eff, RunTools):
                    for call in eff.calls:
                        pending.append(ToolOutcome(result=script.result_for(call)))
                elif isinstance(eff, AskUser):
                    pending.append(
                        PermissionAnswer(
                            call_id=eff.question.call_id,
                            allow=script.answers.get(eff.question.call_id, True),
                        )
                    )
                elif isinstance(eff, Finish):
                    finishes.append(eff.result)
    return SimRun(state=state, events=events, finishes=finishes)


def mk_out(*blocks: Block, usage: Usage = Usage(10, 5)) -> ModelOutput:
    return ModelOutput(blocks=tuple(blocks), usage=usage)


def to_envelopes(events: Iterable[Event], sid: str = "s1") -> tuple:
    return tuple(
        make_envelope(seq=i, sid=sid, body=e, ts=float(i)) for i, e in enumerate(events)
    )


def kinds(events: Iterable[Event]) -> list[str]:
    return [type(e).__name__ for e in events]


def assert_matched_pairs(state: SessionState) -> None:
    """Every assistant tool call is answered by the next message, in order."""
    msgs = state.messages
    for i, msg in enumerate(msgs):
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            assert i + 1 < len(msgs), f"message {i}: tool calls without results"
            nxt = msgs[i + 1]
            assert isinstance(nxt, ToolResultMessage), f"message {i + 1}: expected results"
            assert [r.call_id for r in nxt.results] == [c.id for c in msg.tool_calls]
