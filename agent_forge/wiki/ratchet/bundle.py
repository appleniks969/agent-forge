"""
ratchet/bundle.py — render a session JSONL into a compact LLM bundle.

The session log is verbose (tool calls, raw outputs, thinking blocks).
Ratchet doesn't need that — it needs the *conversational signal*: what the
human asked, what the agent decided, what code/files came up. We boil the
JSONL down to a markdown transcript under a hard char cap.

Reads:  ~/.agent-forge/sessions/<sid>.jsonl  (via session.resume_session)
Writes: nothing — returns a string

The reason this is a separate file from runner.py: the bundle format is
the iteration surface. When the ratchet skill changes what it wants
(e.g. "include tool result snippets, not just messages"), only this file
changes.
"""
from __future__ import annotations

from ...messages import (
    AssistantMessage, TextContent, ThinkingContent, ToolCallContent,
    ToolResultMessage, UserMessage,
)
from ...session import resume_session


def build_session_bundle(session_id: str, *, max_chars: int = 24_000) -> str:
    """Return a markdown transcript of the session, capped at ``max_chars``.

    Layout per turn::

        ## User
        <user message text, possibly truncated>

        ## Assistant
        <text blocks joined>
        <tool calls listed as "→ tool_name(args_summary)">
        <tool results inlined as "← <first_line>" or "← [error]">
    """
    resumed = resume_session(session_id)
    if not resumed.messages:
        return ""

    parts: list[str] = []
    used = 0
    sep = "\n\n"

    def _add(chunk: str) -> bool:
        """Append chunk if it fits. Return True to continue, False to stop."""
        nonlocal used
        cost = len(chunk) + (len(sep) if parts else 0)
        if used + cost > max_chars:
            return False
        parts.append(chunk)
        used += cost
        return True

    # We emit messages in order but drop very large tool results.
    for msg in resumed.messages:
        if isinstance(msg, UserMessage):
            content = _user_content_text(msg)
            if not _add(f"## User\n{_truncate(content, 1500)}"):
                break
        elif isinstance(msg, AssistantMessage):
            chunk = _format_assistant(msg)
            if chunk and not _add(chunk):
                break
        elif isinstance(msg, ToolResultMessage):
            chunk = _format_tool_results(msg)
            if chunk and not _add(chunk):
                break

    return sep.join(parts)


# ── Per-message rendering ─────────────────────────────────────────────────────

def _user_content_text(msg: UserMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    out: list[str] = []
    for blk in msg.content:
        if isinstance(blk, TextContent):
            out.append(blk.text)
        else:  # ImageContent, etc.
            out.append("[image]")
    return "\n".join(out)


def _format_assistant(msg: AssistantMessage) -> str:
    """Render an assistant turn: thinking dropped, text kept, tool calls listed."""
    lines: list[str] = ["## Assistant"]
    text_parts: list[str] = []
    tool_lines: list[str] = []
    for blk in msg.content:
        if isinstance(blk, TextContent):
            text_parts.append(blk.text.strip())
        elif isinstance(blk, ThinkingContent):
            continue  # ratchet doesn't read raw thinking — too noisy
        elif isinstance(blk, ToolCallContent):
            tool_lines.append(f"→ {blk.name}({_summarise_args(blk.arguments)})")
    body = "\n".join(t for t in text_parts if t)
    if body:
        lines.append(_truncate(body, 1500))
    if tool_lines:
        lines.append("\n".join(tool_lines[:8]))
        if len(tool_lines) > 8:
            lines.append(f"… and {len(tool_lines) - 8} more tool calls")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _format_tool_results(msg: ToolResultMessage) -> str:
    """Tool result: one line, error-flagged, body's first line only.

    ``ToolResultMessage`` carries one tool result (per agent_forge.messages);
    multiple tool calls produce multiple ToolResultMessages in sequence.
    """
    if isinstance(msg.content, str):
        first = msg.content.strip().splitlines()
        head = first[0][:120] if first else "(no output)"
    else:
        # Vision-tuple result; take the first text block we find.
        head = "(non-text result)"
        for blk in msg.content:
            if isinstance(blk, TextContent):
                line = blk.text.strip().splitlines()
                if line:
                    head = line[0][:120]
                    break
    prefix = "← [error] " if msg.is_error else "← "
    return f"## Tool results\n{prefix}{head}"


def _summarise_args(args: dict) -> str:
    """Compact one-line arg summary: keep keys, abbreviate string values."""
    pairs: list[str] = []
    for k, v in (args or {}).items():
        if isinstance(v, str):
            v = v.replace("\n", " ")
            v = v[:60] + "…" if len(v) > 60 else v
            pairs.append(f"{k}={v!r}")
        else:
            s = repr(v)
            pairs.append(f"{k}={s[:60]}{'…' if len(s) > 60 else ''}")
    return ", ".join(pairs)


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 16].rstrip() + "\n…[truncated]"
