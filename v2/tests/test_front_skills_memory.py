"""front/memory + commands skill sugar: the /remember write path, /skills
catalog rendering, and the command-vs-skill-vs-unknown dispatch routing.

All file I/O is under tmp_path; nothing here touches the real repo or
~/.agent-forge. The skill resolver and session are stubs so dispatch routing
can be asserted without a live SessionHandle.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

import pytest

from forge.front import commands, memory


# --- memory.remember -----------------------------------------------------------


def _mem_file(tmp_path: Path) -> Path:
    return tmp_path / ".agent-forge" / "memory.md"


def test_remember_appends_stamped_bullet(tmp_path: Path) -> None:
    out = memory.remember(tmp_path, "the build uses uv, not pip")
    assert "remembered" in out
    text = _mem_file(tmp_path).read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    assert f"- the build uses uv, not pip (learned {today})" in text


def test_remember_creates_dir_and_header(tmp_path: Path) -> None:
    assert not _mem_file(tmp_path).exists()
    memory.remember(tmp_path, "first fact")
    text = _mem_file(tmp_path).read_text(encoding="utf-8")
    assert "## Memory" in text
    assert text.endswith("\n")  # trailing newline so the next append is clean


def test_remember_dedups_exact_repeat(tmp_path: Path) -> None:
    fact = "the kernel imports nothing internal per the layer law"
    memory.remember(tmp_path, fact)
    out = memory.remember(tmp_path, fact)  # exact repeat -> duplicate
    assert "already known" in out or "duplicate" in out
    bullets = [
        line
        for line in _mem_file(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("-")
    ]
    assert len(bullets) == 1


def test_remember_dedups_by_60char_prefix(tmp_path: Path) -> None:
    # Two facts that are identical for the first 60 chars of the stamped entry
    # (entry is '- <text> (learned ...)', so >58 chars of shared text) dedup.
    shared = "x" * 70  # well past the 60-char prefix window
    memory.remember(tmp_path, shared + " ending one")
    out = memory.remember(tmp_path, shared + " ending two")
    assert "already known" in out or "duplicate" in out
    bullets = [
        line
        for line in _mem_file(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("-")
    ]
    assert len(bullets) == 1


def test_remember_distinct_facts_both_kept(tmp_path: Path) -> None:
    memory.remember(tmp_path, "alpha fact about the kernel layer")
    memory.remember(tmp_path, "beta fact about the policy layer")
    bullets = [
        line
        for line in _mem_file(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("-")
    ]
    assert len(bullets) == 2


def test_remember_rejects_blank(tmp_path: Path) -> None:
    out = memory.remember(tmp_path, "   ")
    assert "empty" in out or "nothing" in out
    assert not _mem_file(tmp_path).exists()


def test_remember_caps_at_token_budget(tmp_path: Path) -> None:
    # Each learning is a few hundred bytes; write far past the ~8KB cap and
    # assert the file stops growing and the NEWEST learning survives.
    chunk = "x" * 200
    for i in range(120):
        memory.remember(tmp_path, f"fact-{i:03d} {chunk}")
    text = _mem_file(tmp_path).read_text(encoding="utf-8")
    assert len(text.encode()) // 4 <= memory._MEMORY_CAP_TOKENS + 200
    assert "fact-119" in text  # newest kept
    assert "fact-000" not in text  # oldest evicted


# --- /skills rendering ---------------------------------------------------------


@dataclass(frozen=True)
class FakeSkillMeta:
    name: str
    description: str
    path: Path = Path("/x")


def _ctx(**kwargs: object) -> commands.CommandContext:
    base: dict = {"session": _FakeSession(), "model": "m"}
    base.update(kwargs)
    return commands.CommandContext(**base)  # type: ignore[arg-type]


def test_skills_renders_name_dash_description(tmp_path: Path) -> None:
    skills = (
        FakeSkillMeta("deep-research", "fan-out web research"),
        FakeSkillMeta("plan", "produce an implementation plan"),
    )
    text = commands.dispatch("/skills", _ctx(skills=skills)).text
    assert "deep-research — fan-out web research" in text
    assert "plan — produce an implementation plan" in text


def test_skills_empty_says_none(tmp_path: Path) -> None:
    assert commands.dispatch("/skills", _ctx(skills=())).text == "no skills found"
    assert commands.dispatch("/skills", _ctx(skills=None)).text == "no skills found"


def test_skills_accepts_renderer_callable() -> None:
    text = commands.dispatch(
        "/skills", _ctx(skills=lambda: "a — one\nb — two")
    ).text
    assert text == "a — one\nb — two"


def test_skills_in_command_table() -> None:
    names = [c.name for c in commands.COMMANDS]
    assert "skills" in names
    assert "remember" in names


# --- /remember handler ---------------------------------------------------------


def test_remember_command_writes_to_ctx_cwd(tmp_path: Path) -> None:
    out = commands.dispatch("/remember the seam lives in prompt.py", _ctx(cwd=tmp_path))
    assert "remembered" in out.text
    assert "the seam lives in prompt.py" in _mem_file(tmp_path).read_text(
        encoding="utf-8"
    )


def test_remember_command_usage_when_blank(tmp_path: Path) -> None:
    out = commands.dispatch("/remember", _ctx(cwd=tmp_path))
    assert "usage" in out.text
    assert not _mem_file(tmp_path).exists()


# --- dispatch routing: command vs skill vs unknown -----------------------------


class _FakeSession:
    """Records submit() calls so the skill-run path can be asserted."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, text: str) -> None:
        self.submitted.append(text)


def _resolver(catalog: dict[str, str]):
    def resolve(name: str) -> str | None:
        return catalog.get(name)

    return resolve


def test_known_command_wins_over_skill_of_same_name(tmp_path: Path) -> None:
    # A skill named 'remember' must NOT shadow the built-in /remember command:
    # the command runs (writes memory) and the skill body is never submitted.
    session = _FakeSession()
    ctx = commands.CommandContext(
        session=session,  # type: ignore[arg-type]
        model="m",
        cwd=tmp_path,
        skill_resolver=_resolver({"remember": "SKILL BODY"}),
    )
    out = commands.dispatch("/remember a real learning", ctx)
    assert "remembered" in out.text  # command ran
    assert "SKILL BODY" not in out.text
    assert session.submitted == []  # skill never invoked
    assert _mem_file(tmp_path).exists()


async def test_unknown_skill_token_runs_skill() -> None:
    session = _FakeSession()
    ctx = commands.CommandContext(
        session=session,  # type: ignore[arg-type]
        model="m",
        skill_resolver=_resolver({"deep-research": "Do the research thoroughly."}),
    )
    out = commands.dispatch("/deep-research compare X and Y", ctx)
    assert out.action is not None  # async submit thunk
    assert out.text == ""
    result = await out.action()
    assert result == ""
    assert len(session.submitted) == 1
    sent = session.submitted[0]
    assert "Do the research thoroughly." in sent  # body injected
    assert "compare X and Y" in sent  # args carried through
    assert "deep-research" in sent  # name framed in the delimiter
    # Body must be delimited so the model separates instructions from the turn.
    assert "<skill-instructions>" in sent and "</skill-instructions>" in sent


async def test_skill_without_args_still_submits_body() -> None:
    session = _FakeSession()
    ctx = commands.CommandContext(
        session=session,  # type: ignore[arg-type]
        model="m",
        skill_resolver=_resolver({"plan": "Write a plan."}),
    )
    out = commands.dispatch("/plan", ctx)
    assert out.action is not None
    await out.action()
    assert "Write a plan." in session.submitted[0]


def test_unknown_token_neither_command_nor_skill() -> None:
    ctx = commands.CommandContext(
        session=_FakeSession(),  # type: ignore[arg-type]
        model="m",
        skill_resolver=_resolver({"plan": "Write a plan."}),
    )
    out = commands.dispatch("/nope", ctx)
    assert "unknown" in out.text and "/help" in out.text
    assert out.action is None


def test_unknown_token_without_resolver_is_unknown() -> None:
    # Backward compatible: no skill_resolver -> old unknown-command behavior.
    ctx = commands.CommandContext(session=_FakeSession(), model="m")  # type: ignore[arg-type]
    out = commands.dispatch("/deep-research", ctx)
    assert "unknown" in out.text


@pytest.mark.parametrize("missing_field", ["cwd", "skills", "skill_resolver"])
def test_new_context_fields_default_none(missing_field: str) -> None:
    # New fields are optional so existing CommandContext(...) callers keep working.
    ctx = commands.CommandContext(session=_FakeSession(), model="m")  # type: ignore[arg-type]
    assert getattr(ctx, missing_field) is None
