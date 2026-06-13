"""Orientation section thunks (agents_doc/repo_map/memory/skills): pure
wrappers over injected suppliers — correct name + SESSION stability, pass-through
of the supplier (including None), and the cache-correctness property via
PromptAssembler.build()."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from forge.kernel.types import Stability
from forge.policy.prompt import (
    PromptAssembler,
    agents_doc_section,
    memory_section,
    repo_map_section,
    skills_section,
)

BUILDERS: tuple[tuple[str, Callable], ...] = (
    ("agents_doc", agents_doc_section),
    ("repo_map", repo_map_section),
    ("memory", memory_section),
    ("skills", skills_section),
)


# --- name + stability ------------------------------------------------------------


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_builder_yields_named_session_thunk(name: str, builder: Callable) -> None:
    section = builder(lambda: "x")
    assert section.name == name
    assert section.stability is Stability.SESSION


# --- pass-through of the supplier (including None) -------------------------------


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_resolve_passes_supplier_output_through(name: str, builder: Callable) -> None:
    assert builder(lambda: "loaded text").resolve() == "loaded text"


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_resolve_passes_none_through(name: str, builder: Callable) -> None:
    assert builder(lambda: None).resolve() is None


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_resolve_is_reresolved_per_call(name: str, builder: Callable) -> None:
    source: dict[str, str | None] = {"text": "v1"}
    section = builder(lambda: source["text"])
    assert section.resolve() == "v1"
    source["text"] = "v2"
    assert section.resolve() == "v2"


# --- appears / omitted through PromptAssembler.build() --------------------------


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_section_appears_when_supplier_returns_text(name: str, builder: Callable) -> None:
    assembler = PromptAssembler([builder(lambda: "orientation body")])
    sections = assembler.build()
    assert [s.name for s in sections] == [name]
    assert sections[0].text == "orientation body"


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_section_omitted_when_supplier_returns_none(name: str, builder: Callable) -> None:
    assert PromptAssembler([builder(lambda: None)]).build() == ()


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_section_omitted_when_supplier_returns_blank(name: str, builder: Callable) -> None:
    # whitespace-only output is omitted, same as the other builders
    assert PromptAssembler([builder(lambda: "   \n  ")]).build() == ()


# --- cache-correctness property -------------------------------------------------


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_unchanged_output_yields_identical_section_object(
    name: str, builder: Callable
) -> None:
    # fresh str object each call, same bytes — content hash must memoize so
    # downstream caches can key on section identity.
    section = builder(lambda: "stable " + "body")
    assembler = PromptAssembler([section])
    first = assembler.build()[0]
    second = assembler.build()[0]
    assert second is first


@pytest.mark.parametrize("name,builder", BUILDERS)
def test_changed_output_invalidates_the_memo(name: str, builder: Callable) -> None:
    source: dict[str, str | None] = {"text": "before"}
    assembler = PromptAssembler([builder(lambda: source["text"])])
    first = assembler.build()[0]
    source["text"] = "after"
    second = assembler.build()[0]
    assert second is not first
    assert second.text == "after"
