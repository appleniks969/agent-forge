"""Default secret redactor + wiring rewrite-before-append."""

from __future__ import annotations

from pathlib import Path

from forge.adapters.jsonl_store import JsonlStore
from forge.adapters.redact import redact_text, secret_redactor
from forge.kernel.events import ToolFinished, UserSubmitted, make_envelope
from forge.kernel.types import ToolResult


def test_redact_text_masks_common_secrets() -> None:
    assert "[redacted]" in redact_text("token=sk-ant-abcdefghijk")
    assert "sk-ant-abcdefghijk" not in redact_text("token=sk-ant-abcdefghijk")
    assert "ghp_abcdefghijklmnopqrstuv" not in redact_text("auth ghp_abcdefghijklmnopqrstuv")
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_text("id=AKIAIOSFODNN7EXAMPLE")
    assert "Bearer secret-token-value" not in redact_text("Authorization: Bearer secret-token-value")
    assert "hunter22" not in redact_text("api_key=hunter22")


def test_secret_redactor_rewrites_tool_result() -> None:
    ev = secret_redactor(
        ToolFinished(ToolResult("c1", "ANTHROPIC_API_KEY=sk-ant-abcdefghijk"))
    )
    assert isinstance(ev, ToolFinished)
    assert "sk-ant-abcdefghijk" not in ev.result.content
    assert "[redacted]" in ev.result.content


def test_wired_redactor_strips_secrets_from_disk(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path, "s1", redactor=secret_redactor)
    secret = "sk-ant-abcdefghijk"
    store.append(
        make_envelope(0, "s1", ToolFinished(ToolResult("c1", f"key={secret}")))
    )
    raw = store.path.read_text(encoding="utf-8")
    assert secret not in raw
    replayed = store.replay()
    assert isinstance(replayed[0].body, ToolFinished)
    assert secret not in replayed[0].body.result.content
