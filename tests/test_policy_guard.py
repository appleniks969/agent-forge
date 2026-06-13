"""Guard policy: table-driven standard guards, chain combinator semantics, and
StandardPolicy.judge integration."""

from __future__ import annotations

from pathlib import PurePath, PurePosixPath
from typing import Any

import pytest

from forge.kernel.types import (
    Allow,
    Ask,
    Deny,
    Effects,
    PermissionQuestion,
    ToolCall,
    Verdict,
)
from forge.policy import StandardPolicy
from forge.policy.guard import (
    STANDARD_GUARDS,
    GuardChain,
    chain,
    destructive_bash_guard,
    external_guard,
    guards_from,
    judge,
    sensitive_path_guard,
)

WS = PurePosixPath("/ws")


def call(name: str = "bash", **args: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, args=args)


# --- destructive bash (heuristic floor) ----------------------------------------


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("ls -la", False),
        ("rm build/tmp.txt", False),
        ("rm -rf ./build", False),
        ("sudo apt install x", True),
        ("rm -rf /", True),
        ("rm -fr /", True),
        ("rm -r -f /", True),
        ("rm -rf ~", True),
        ("rm -rf $HOME", True),
        ("git push origin main", False),
        ("git push -f origin main", True),
        ("git push origin main --force", True),
        ("git reset --hard origin/main", True),
        ("git reset --hard HEAD~1", False),
        (":(){ :|:& };:", True),
        ("echo safe && sudo reboot", True),  # patterns match anywhere
    ],
)
def test_destructive_bash_table(command: str, blocked: bool) -> None:
    verdict = destructive_bash_guard(call(command=command), Effects.EXEC, WS)
    assert isinstance(verdict, Deny) == blocked
    if not blocked:
        assert verdict is None  # abstains rather than explicitly allowing


def test_bash_guard_keys_on_exec_effects_not_tool_name() -> None:
    # a renamed shell tool is still guarded; a non-EXEC tool is not
    assert isinstance(
        destructive_bash_guard(call(name="shell", command="sudo x"), Effects.EXEC, WS),
        Deny,
    )
    assert destructive_bash_guard(call(command="sudo x"), Effects.READ_PATH, WS) is None


def test_bash_guard_abstains_without_command_shaped_args() -> None:
    assert destructive_bash_guard(call(pattern="sudo"), Effects.EXEC, WS) is None


# --- sensitive path writes -------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "blocked"),
    [
        ("src/main.py", False),
        ("/ws/src/app.py", False),
        ("/etc/passwd", True),
        ("/etcetera/notes.txt", False),  # prefix match is segment-aware
        ("/usr/bin/python", True),
        ("/boot/grub.cfg", True),
        ("~/.ssh/config", True),
        (".aws/credentials", True),  # relative path, sensitive segment
        ("../../etc/hosts", True),  # normalizes to /etc/hosts from /ws
        ("conf/.gnupg/keys", True),
        ("docs/ssh-notes.md", False),
    ],
)
def test_sensitive_path_table(path: str, blocked: bool) -> None:
    verdict = sensitive_path_guard(
        call(name="write", path=path, content="data"), Effects.WRITE_PATH, WS
    )
    assert isinstance(verdict, Deny) == blocked


def test_path_guard_keys_on_write_effects_only() -> None:
    assert (
        sensitive_path_guard(call(path="/etc/passwd"), Effects.READ_PATH, WS) is None
    )


def test_path_guard_ignores_non_path_args() -> None:
    # content mentioning /etc must not trip the guard; only path-shaped keys do
    verdict = sensitive_path_guard(
        call(name="write", path="notes.md", content="see /etc/hosts and ~/.ssh"),
        Effects.WRITE_PATH,
        WS,
    )
    assert verdict is None


# --- EXTERNAL => Ask ---------------------------------------------------------------


def test_external_effects_default_to_ask() -> None:
    verdict = external_guard(
        call(name="github__create_issue", title="x"), Effects.EXTERNAL, WS
    )
    assert isinstance(verdict, Ask)
    assert verdict.question.call_id == "c1"
    assert verdict.question.tool == "github__create_issue"
    assert verdict.question.question


def test_external_guard_abstains_for_local_effects() -> None:
    for effects in (Effects.READ_PATH, Effects.WRITE_PATH, Effects.EXEC, Effects.NETWORK):
        assert external_guard(call(), effects, WS) is None


def test_unannotated_mcp_default_effects_reach_ask() -> None:
    # the MCP manager maps unannotated tools to WRITE_PATH|EXEC|EXTERNAL
    effects = Effects.WRITE_PATH | Effects.EXEC | Effects.EXTERNAL
    assert isinstance(judge(call(name="srv__do_thing"), effects, WS), Ask)


# --- chain combinator ---------------------------------------------------------------


def _deny(reason: str):
    def guard(c: ToolCall, e: Effects, w: PurePath) -> Verdict | None:
        return Deny(reason)

    return guard


def _ask(c: ToolCall, e: Effects, w: PurePath) -> Verdict | None:
    return Ask(PermissionQuestion(c.id, c.name, "sure?"))


def _abstain(c: ToolCall, e: Effects, w: PurePath) -> Verdict | None:
    return None


def test_first_deny_wins_in_order() -> None:
    verdict = chain(_abstain, _deny("first"), _deny("second")).judge(
        call(), Effects.EXEC, WS
    )
    assert verdict == Deny("first")


def test_deny_beats_ask_regardless_of_position() -> None:
    assert chain(_ask, _deny("no")).judge(call(), Effects.EXEC, WS) == Deny("no")
    assert chain(_deny("no"), _ask).judge(call(), Effects.EXEC, WS) == Deny("no")


def test_ask_beats_allow_when_no_deny() -> None:
    verdict = chain(_abstain, _ask).judge(call(), Effects.EXEC, WS)
    assert isinstance(verdict, Ask)


def test_all_abstain_means_allow() -> None:
    assert chain(_abstain, _abstain).judge(call(), Effects.EXEC, WS) == Allow()
    assert GuardChain(()).judge(call(), Effects.EXEC, WS) == Allow()


def test_all_guards_observe_even_after_a_deny() -> None:
    seen: list[str] = []

    def observer(tag: str):
        def guard(c: ToolCall, e: Effects, w: PurePath) -> Verdict | None:
            seen.append(tag)
            return None

        return guard

    verdict = chain(observer("a"), _deny("stop"), observer("b")).judge(
        call(), Effects.EXEC, WS
    )
    assert verdict == Deny("stop")
    assert seen == ["a", "b"]  # audit saw the call despite the deny


def test_guards_from_puts_standard_floor_first() -> None:
    extra_chain = guards_from((_ask,))
    assert extra_chain.guards[: len(STANDARD_GUARDS)] == STANDARD_GUARDS
    # the floor still denies even though the extra guard would only ask
    verdict = extra_chain.judge(call(command="sudo x"), Effects.EXEC, WS)
    assert isinstance(verdict, Deny)


# --- module-level judge + StandardPolicy integration --------------------------------


@pytest.mark.parametrize(
    ("c", "effects", "expected"),
    [
        (call(command="ls"), Effects.EXEC, Allow),
        (call(command="sudo rm x"), Effects.EXEC, Deny),
        (call(name="write", path="/etc/crontab"), Effects.WRITE_PATH, Deny),
        (call(name="srv__post"), Effects.EXTERNAL, Ask),
        (call(name="read", path="src/a.py"), Effects.READ_PATH, Allow),
    ],
)
def test_judge_standard_table(c: ToolCall, effects: Effects, expected: type) -> None:
    assert isinstance(judge(c, effects, WS), expected)


def test_standard_policy_judges_with_ws_root() -> None:
    policy = StandardPolicy(model="m", context_tokens=100_000, tools=(), ws_root=WS)
    assert isinstance(
        policy.judge(call(name="write", path="../../etc/x"), Effects.WRITE_PATH), Deny
    )
    assert isinstance(policy.judge(call(name="srv__x"), Effects.EXTERNAL), Ask)
    assert policy.judge(call(name="read", path="a.py"), Effects.READ_PATH) == Allow()
    assert policy.judge(call(), Effects(0)) == Allow()  # unknown tool, no effects
