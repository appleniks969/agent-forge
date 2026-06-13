"""Prompt policy: thunk re-resolution, content-hash memoization, stability
tags, and the standard section builders."""

from __future__ import annotations

from forge.kernel.types import Effects, Stability, ToolSpec
from forge.policy.prompt import (
    DEFAULT_IDENTITY,
    PromptAssembler,
    SectionThunk,
    content_digest,
    environment_section,
    identity_section,
    tools_section,
)


def thunk(name: str, source: dict[str, str | None], stability: Stability = Stability.SESSION) -> SectionThunk:
    return SectionThunk(name, stability, lambda: source["text"])


# --- re-resolution (capture-by-value is structurally gone) ----------------------


def test_thunks_are_reresolved_at_every_build() -> None:
    source: dict[str, str | None] = {"text": "memory v1"}
    assembler = PromptAssembler([thunk("memory", source)])
    assert assembler.build()[0].text == "memory v1"
    source["text"] = "memory v2"  # e.g. /remember rewrote the file
    assert assembler.build()[0].text == "memory v2"


def test_section_can_appear_and_disappear_between_builds() -> None:
    source: dict[str, str | None] = {"text": None}
    assembler = PromptAssembler([thunk("wiki", source)])
    assert assembler.build() == ()
    source["text"] = "wiki content"
    assert [s.name for s in assembler.build()] == ["wiki"]
    source["text"] = "   "  # whitespace-only is omitted too
    assert assembler.build() == ()


# --- content-hash memoization -----------------------------------------------------


def test_unchanged_content_yields_the_identical_section_object() -> None:
    source: dict[str, str | None] = {"text": "stable"}
    assembler = PromptAssembler([thunk("memory", source)])
    first = assembler.build()[0]
    second = assembler.build()[0]
    assert second is first  # memo hit: downstream caches can key on identity


def test_changed_content_invalidates_the_memo() -> None:
    source: dict[str, str | None] = {"text": "v1"}
    assembler = PromptAssembler([thunk("memory", source)])
    first = assembler.build()[0]
    source["text"] = "v2"
    second = assembler.build()[0]
    assert second is not first
    assert second.text == "v2"


def test_memo_keys_on_content_not_resolve_count() -> None:
    # the thunk "touches" its source every build (new string object, same
    # bytes) — an mtime-style key would invalidate; a content hash must not.
    counter = {"n": 0}

    def resolve() -> str:
        counter["n"] += 1
        return "same " + "content"  # fresh str object each call

    assembler = PromptAssembler([SectionThunk("doc", Stability.SESSION, resolve)])
    first = assembler.build()[0]
    second = assembler.build()[0]
    assert counter["n"] == 2  # resolved twice...
    assert second is first  # ...but memoized on content


def test_memo_is_per_section_name() -> None:
    a: dict[str, str | None] = {"text": "alpha"}
    b: dict[str, str | None] = {"text": "beta"}
    assembler = PromptAssembler([thunk("a", a), thunk("b", b)])
    first = assembler.build()
    b["text"] = "beta 2"
    second = assembler.build()
    assert second[0] is first[0]  # untouched section keeps its identity
    assert second[1] is not first[1]


def test_content_digest_is_deterministic_and_content_sensitive() -> None:
    assert content_digest("x") == content_digest("x")
    assert content_digest("x") != content_digest("y")


# --- stability tags + builders -----------------------------------------------------


def test_builders_carry_their_stability_tags() -> None:
    assert identity_section().stability is Stability.STATIC
    assert environment_section(lambda: {}).stability is Stability.VOLATILE
    assert tools_section(lambda: ()).stability is Stability.SESSION


def test_identity_section_default_text() -> None:
    section = PromptAssembler([identity_section()]).build()[0]
    assert section.name == "identity"
    assert section.text == DEFAULT_IDENTITY


def test_environment_section_renders_facts_and_reresolves() -> None:
    facts = {"cwd": "/ws", "platform": "darwin"}
    assembler = PromptAssembler([environment_section(lambda: dict(facts))])
    text = assembler.build()[0].text
    assert "cwd: /ws" in text and "platform: darwin" in text
    facts["cwd"] = "/elsewhere"
    assert "cwd: /elsewhere" in assembler.build()[0].text


def test_environment_section_omitted_when_no_facts() -> None:
    assert PromptAssembler([environment_section(lambda: {})]).build() == ()


def test_tools_section_renders_specs_with_effects() -> None:
    specs = (
        ToolSpec(name="read", description="read  a\nfile", effects=Effects.READ_PATH),
        ToolSpec(
            name="bash",
            description="run a command",
            effects=Effects.EXEC | Effects.WRITE_PATH,
        ),
        ToolSpec(name="noop", description="does nothing"),
    )
    text = PromptAssembler([tools_section(lambda: specs)]).build()[0].text
    assert "- read: read a file [read_path]" in text  # whitespace collapsed
    assert "- bash: run a command [write_path|exec]" in text
    assert "- noop: does nothing" in text  # no effects -> no bracket


def test_tools_section_omitted_when_no_tools() -> None:
    assert PromptAssembler([tools_section(lambda: ())]).build() == ()
