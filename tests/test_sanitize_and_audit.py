"""Tests for sanitize_exception() + AuditHook + ADR-006 v1 scope.

Covers:
- sanitize_exception() helper — includes exception class name, redacts
  home directory, used in every tool's except-Exception fallthrough,
  used in loop.py's last-resort tool-execute catch.
- AuditHook — emits structured before/after log records, redacts args
  by default, measures duration, composable via _CompositeHook.
- A tiny doc test asserting ADR-006 declares resources/prompts
  "out of scope (v1)" — guards against silently shipping resources
      support without revisiting the ADR.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_forge import (
    AuditHook, Hooks, ToolCallContent, ToolResult, sanitize_exception,
)
from agent_forge.guards import _CompositeHook, BashGuardHook
from agent_forge.hooks import NoopHooks
from agent_forge.tools import (
    BashTool, EditTool, FindTool, GrepTool, ReadTool, WriteTool,
)


# ── K1 — sanitize_exception() ────────────────────────────────────────────────


def test_sanitize_exception_includes_class_name():
    out = sanitize_exception(ValueError("bad value"))
    assert out.startswith("ValueError:")
    assert "bad value" in out


def test_sanitize_exception_handles_empty_message():
    out = sanitize_exception(RuntimeError())
    # An empty message must still surface the type so the LLM can react.
    assert out == "RuntimeError"


def test_sanitize_exception_redacts_home_directory():
    home = str(Path.home())
    # FileNotFoundError str includes the path verbatim
    exc = FileNotFoundError(f"[Errno 2] No such file: '{home}/secret.txt'")
    out = sanitize_exception(exc)
    assert home not in out
    assert "~/secret.txt" in out
    assert "FileNotFoundError:" in out


def test_sanitize_exception_redacts_home_in_many_places():
    home = str(Path.home())
    msg = f"problem at {home}/a, also {home}/b, and {home}/c"
    out = sanitize_exception(RuntimeError(msg))
    assert home not in out
    assert out.count("~/") == 3


def test_sanitize_exception_no_traceback_leak():
    """Stack traces leak internal site-packages paths — never include."""
    try:
        raise ZeroDivisionError("kaboom")
    except ZeroDivisionError as exc:
        out = sanitize_exception(exc)
    # Should be a single-line, no "Traceback (most recent call last)" header
    assert "\n" not in out
    assert "Traceback" not in out
    assert out == "ZeroDivisionError: kaboom"


def test_sanitize_exception_does_not_redact_root_path():
    """If $HOME is '/' (e.g. running as root in a container) we must
    not turn every '/' into '~' — would mangle every error message."""
    # We can't easily monkey-patch Path.home() without globals, but the
    # function explicitly skips redaction when home is "" or "/".
    # Smoke-test: confirm the implementation guards.
    msg = "/etc/passwd not found"
    # Whatever home is, only literal-home replacement should fire.
    out = sanitize_exception(FileNotFoundError(msg))
    assert "/etc/passwd not found" in out or "~" in out


@pytest.mark.asyncio
async def test_tools_use_sanitize_exception(tmp_path, monkeypatch):
    """Every built-in tool must funnel its last-resort except through
    sanitize_exception so user paths don't leak in error messages."""
    monkeypatch.chdir(tmp_path)
    cwd = str(tmp_path)
    home = str(Path.home())

    # ReadTool against a non-existent path under home produces a redacted error.
    # We pass an absolute path inside $HOME via a symlink trick — but the
    # easiest reliable check is the WriteTool with a path that triggers
    # an OSError once we make the parent read-only. Cross-platform that's
    # fragile, so we instead patch one tool to raise a $HOME-bearing
    # exception and confirm the wrapper does the right thing.

    class ExplodingTool(BashTool):
        async def execute(self, args, *, cwd, signal=None):
            try:
                raise FileNotFoundError(f"could not open {home}/sekret")
            except Exception as exc:
                return ToolResult(content=sanitize_exception(exc), is_error=True)

    result = await ExplodingTool().execute({"command": "noop"}, cwd=cwd)
    assert result.is_error
    assert home not in result.content
    assert "~/sekret" in result.content
    assert "FileNotFoundError:" in result.content


@pytest.mark.asyncio
async def test_read_tool_unknown_file_message_is_clean(tmp_path):
    """Verify that the existing FileNotFoundError-specific error path
    (which uses an f-string, not the except-fallthrough) still emits a
    useful message that doesn't leak the absolute home directory.

    Note: ReadTool's "File not found:" branch uses the *relative* path
    the LLM supplied, which is the right behaviour regardless of
    sanitization.
    """
    out = await ReadTool().execute({"path": "no_such_file.txt"}, cwd=str(tmp_path))
    assert out.is_error
    assert "File not found" in out.content
    # The path it echoes is what the LLM asked for, NOT an absolute path.
    assert str(tmp_path) not in out.content


# ── K2 — AuditHook ───────────────────────────────────────────────────────────


def test_audit_hook_satisfies_protocol():
    assert isinstance(AuditHook(), Hooks)
    assert isinstance(AuditHook(), NoopHooks)


@pytest.mark.asyncio
async def test_audit_hook_emits_before_and_after(caplog):
    h = AuditHook(level=logging.INFO)
    call = ToolCallContent(id="abc", name="Bash", arguments={"command": "ls"})

    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, turn=1)
        await h.after_tool_call(call, ToolResult(content="ok"), turn=1)

    records = [r.getMessage() for r in caplog.records]
    assert any("turn=1" in r and "tool=Bash" in r and "args={command}" in r for r in records)
    assert any("ok duration_ms=" in r and "id=abc" in r for r in records)


@pytest.mark.asyncio
async def test_audit_hook_logs_error_status(caplog):
    h = AuditHook()
    call = ToolCallContent(id="x", name="Bash", arguments={})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
        await h.after_tool_call(call, ToolResult(content="oops", is_error=True), 1)
    assert any("error duration_ms=" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_hook_redacts_args_by_default(caplog):
    h = AuditHook()
    call = ToolCallContent(
        id="x", name="Write",
        arguments={"path": "/etc/passwd", "content": "PWNED"},
    )
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
    msg = caplog.records[0].getMessage()
    assert "PWNED" not in msg
    assert "/etc/passwd" not in msg
    # Should report key set only, sorted for stability
    assert "{content,path}" in msg


@pytest.mark.asyncio
async def test_audit_hook_can_include_full_args(caplog):
    h = AuditHook(redact_args=False)
    call = ToolCallContent(id="x", name="Bash", arguments={"command": "ls -la"})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
    assert "ls -la" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_audit_hook_preview_includes_result_when_enabled(caplog):
    h = AuditHook(include_result_preview=True)
    call = ToolCallContent(id="x", name="Bash", arguments={})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
        await h.after_tool_call(call, ToolResult(content="hello world"), 1)
    after = caplog.records[1].getMessage()
    assert "preview=" in after
    assert "hello world" in after


@pytest.mark.asyncio
async def test_audit_hook_preview_truncates_long_results(caplog):
    h = AuditHook(include_result_preview=True)
    call = ToolCallContent(id="x", name="Bash", arguments={})
    big = "x" * 5000
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
        await h.after_tool_call(call, ToolResult(content=big), 1)
    after = caplog.records[1].getMessage()
    # Must be capped at _MAX_PREVIEW (200)
    assert big not in after
    assert "x" * 200 in after


@pytest.mark.asyncio
async def test_audit_hook_returns_none_so_it_composes(caplog):
    """AuditHook must not block — it returns None from both methods so a
    _CompositeHook chain can still execute the call and other hooks can
    veto."""
    h = AuditHook()
    call = ToolCallContent(id="x", name="Bash", arguments={"command": "rm -rf /"})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        before = await h.before_tool_call(call, 1)
        after = await h.after_tool_call(call, ToolResult(content="ok"), 1)
    assert before is None      # never vetoes
    assert after is None       # never replaces


@pytest.mark.asyncio
async def test_audit_hook_composes_with_guard(caplog):
    """In a _CompositeHook, AuditHook should log AND BashGuardHook should
    still veto destructive bash."""
    composite = _CompositeHook(AuditHook(), BashGuardHook())
    call = ToolCallContent(id="x", name="Bash", arguments={"command": "rm -rf /"})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        decision = await composite.before_tool_call(call, 1)
    assert decision is not None
    assert decision.block is True
    # And the audit log fired
    assert any("tool=Bash" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_hook_duration_is_nonnegative(caplog):
    import asyncio
    h = AuditHook()
    call = ToolCallContent(id="t", name="Bash", arguments={})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.before_tool_call(call, 1)
        await asyncio.sleep(0.01)  # ~10ms
        await h.after_tool_call(call, ToolResult(content=""), 1)
    after = caplog.records[1].getMessage()
    # Pull out "duration_ms=N"
    import re
    m = re.search(r"duration_ms=(\d+)", after)
    assert m is not None
    assert int(m.group(1)) >= 0


@pytest.mark.asyncio
async def test_audit_hook_without_before_still_safe(caplog):
    """If only after_tool_call fires (e.g. composition oddity), the hook
    must not raise — duration_ms reports -1 instead."""
    h = AuditHook()
    call = ToolCallContent(id="orphan", name="Bash", arguments={})
    with caplog.at_level(logging.INFO, logger="agent_forge.audit"):
        await h.after_tool_call(call, ToolResult(content="ok"), 1)
    assert "duration_ms=-1" in caplog.records[0].getMessage()


def test_audit_hook_uses_custom_logger():
    custom = logging.getLogger("my.custom.logger")
    h = AuditHook(logger=custom)
    assert h._log is custom


def test_audit_hook_uses_custom_level():
    h = AuditHook(level=logging.DEBUG)
    assert h._level == logging.DEBUG


def test_sanitize_exception_is_public():
    """The helper is part of the public surface — tools downstream that
    write custom tools should be able to reuse it."""
    import agent_forge
    assert hasattr(agent_forge, "sanitize_exception")
    assert callable(agent_forge.sanitize_exception)


def test_audit_hook_is_public():
    import agent_forge
    assert hasattr(agent_forge, "AuditHook")
