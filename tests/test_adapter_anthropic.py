"""AnthropicProvider tests: mocked at the SDK boundary, no network.

A FakeSDKClient stands in for anthropic.AsyncAnthropic: it records the kwargs
passed to messages.stream() and replays scripted raw SSE-shaped events (or
raises a scripted SDK exception). Covers block assembly, JSON repair,
cache_control placement, OAuth dispatch, effort mapping, transient/fatal
classification, on_delta sequencing, and the provider conformance kit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from forge.adapters.anthropic import (
    OAUTH_IDENTITY,
    AnthropicProvider,
    _repair_json,
)
from forge.kernel.types import (
    AssistantMessage,
    Delta,
    Effects,
    Effort,
    ModelRequest,
    PromptSection,
    Stability,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)
from forge.ports.provider import FatalProviderError, TransientProviderError
from forge.testing import check_provider_contract

# --- SDK-boundary fakes -------------------------------------------------------


class _FakeEventIter:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self) -> _FakeEventIter:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeStreamCM:
    def __init__(self, events: list[Any], error: Exception | None) -> None:
        self._events = events
        self._error = error

    async def __aenter__(self) -> _FakeEventIter:
        if self._error is not None:
            raise self._error
        return _FakeEventIter(self._events)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeMessages:
    def __init__(self, events: list[Any], error: Exception | None) -> None:
        self._events = events
        self._error = error
        self.kwargs: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamCM:
        self.kwargs = kwargs
        self.calls.append(kwargs)
        return _FakeStreamCM(self._events, self._error)


class FakeSDKClient:
    def __init__(self, events: list[Any] | None = None, error: Exception | None = None) -> None:
        self.messages = _FakeMessages(events or [], error)


def ev(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def usage_ns(**kw: int) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def happy_events() -> list[Any]:
    """A full stream: thinking block, text block, one tool call, usage, stop."""
    return [
        ev(
            type="message_start",
            message=ev(
                usage=usage_ns(
                    input_tokens=100,
                    output_tokens=1,
                    cache_read_input_tokens=20,
                    cache_creation_input_tokens=10,
                )
            ),
        ),
        ev(type="content_block_start", index=0, content_block=ev(type="thinking", thinking="")),
        ev(type="content_block_delta", index=0, delta=ev(type="thinking_delta", thinking="let me ")),
        ev(type="content_block_delta", index=0, delta=ev(type="thinking_delta", thinking="see")),
        ev(type="content_block_delta", index=0, delta=ev(type="signature_delta", signature="sig==")),
        ev(type="content_block_stop", index=0),
        ev(type="content_block_start", index=1, content_block=ev(type="text", text="")),
        ev(type="content_block_delta", index=1, delta=ev(type="text_delta", text="Hello ")),
        ev(type="content_block_delta", index=1, delta=ev(type="text_delta", text="world")),
        ev(type="content_block_stop", index=1),
        ev(
            type="content_block_start",
            index=2,
            content_block=ev(type="tool_use", id="tc_1", name="read", input={}),
        ),
        ev(type="content_block_delta", index=2, delta=ev(type="input_json_delta", partial_json='{"path": ')),
        ev(type="content_block_delta", index=2, delta=ev(type="input_json_delta", partial_json='"/tmp/x.txt"}')),
        ev(type="content_block_stop", index=2),
        ev(type="message_delta", delta=ev(stop_reason="tool_use"), usage=usage_ns(output_tokens=42)),
        ev(type="message_stop"),
    ]


def make_req(**over: Any) -> ModelRequest:
    defaults: dict[str, Any] = dict(
        model="claude-sonnet-4-5-20250929",
        system=(PromptSection("identity", "you are forge", Stability.STATIC),),
        messages=(UserMessage("hi"),),
        tools=(
            ToolSpec(
                "read",
                "read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
                Effects.READ_PATH,
            ),
        ),
        effort=Effort.NONE,
        purpose="turn",
    )
    defaults.update(over)
    return ModelRequest(**defaults)


def make_provider(
    events: list[Any] | None = None,
    error: Exception | None = None,
    api_key: str = "sk-ant-api03-test",
    **kw: Any,
) -> tuple[AnthropicProvider, FakeSDKClient]:
    client = FakeSDKClient(events if events is not None else happy_events(), error)
    return AnthropicProvider(api_key, client=client, **kw), client


def status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        f"http {code}", response=httpx.Response(code, request=request), body=None
    )


# --- block assembly -------------------------------------------------------------


async def test_block_assembly_full_stream() -> None:
    provider, _ = make_provider()
    out = await provider.complete(make_req())
    assert out.blocks == (
        ThinkingBlock("let me see"),
        TextBlock("Hello world"),
        ToolCall(id="tc_1", name="read", args={"path": "/tmp/x.txt"}),
    )
    assert out.stop_reason == "tool_use"


async def test_usage_merged_from_start_and_delta() -> None:
    provider, _ = make_provider()
    out = await provider.complete(make_req())
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 42  # message_delta supersedes message_start
    assert out.usage.cache_read_tokens == 20
    assert out.usage.cache_write_tokens == 10


async def test_unclosed_block_is_finalized_at_stream_end() -> None:
    events = [
        ev(type="content_block_start", index=0, content_block=ev(type="text", text="")),
        ev(type="content_block_delta", index=0, delta=ev(type="text_delta", text="partial")),
        ev(type="message_stop"),  # no content_block_stop for index 0
    ]
    provider, _ = make_provider(events)
    out = await provider.complete(make_req())
    assert out.blocks == (TextBlock("partial"),)


async def test_unknown_block_kinds_are_dropped() -> None:
    events = [
        ev(type="content_block_start", index=0, content_block=ev(type="redacted_thinking", data="x")),
        ev(type="content_block_stop", index=0),
        ev(type="content_block_start", index=1, content_block=ev(type="text", text="")),
        ev(type="content_block_delta", index=1, delta=ev(type="text_delta", text="ok")),
        ev(type="content_block_stop", index=1),
        ev(type="message_stop"),
    ]
    provider, _ = make_provider(events)
    out = await provider.complete(make_req())
    assert out.blocks == (TextBlock("ok"),)


async def test_surrogates_sanitized_in_deltas_and_blocks() -> None:
    events = [
        ev(type="content_block_start", index=0, content_block=ev(type="text", text="")),
        ev(type="content_block_delta", index=0, delta=ev(type="text_delta", text="a\ud800b")),
        ev(type="content_block_stop", index=0),
        ev(type="message_stop"),
    ]
    provider, _ = make_provider(events)
    seen: list[Delta] = []
    out = await provider.complete(make_req(), on_delta=seen.append)
    assert out.blocks == (TextBlock("a�b"),)
    assert seen == [Delta("text", "a�b")]


# --- on_delta sequencing ----------------------------------------------------------


async def test_on_delta_sequencing() -> None:
    provider, _ = make_provider()
    seen: list[Delta] = []
    await provider.complete(make_req(), on_delta=seen.append)
    assert seen == [
        Delta("thinking", "let me "),
        Delta("thinking", "see"),
        Delta("text", "Hello "),
        Delta("text", "world"),
    ]  # tool-arg JSON never reaches on_delta


async def test_on_delta_optional() -> None:
    provider, _ = make_provider()
    out = await provider.complete(make_req())  # on_delta=None must not crash
    assert out.text == "Hello world"


# --- streaming JSON repair ---------------------------------------------------------


def test_repair_valid_passthrough() -> None:
    assert _repair_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_repair_empty_and_whitespace() -> None:
    assert _repair_json("") == {}
    assert _repair_json("   \n") == {}


def test_repair_truncated_string_value() -> None:
    assert _repair_json('{"path": "/tmp/fi') == {"path": "/tmp/fi"}


def test_repair_braces_inside_string_are_not_counted() -> None:
    # naive bracket counting would append a spurious closer here
    assert _repair_json('{"cmd": "echo {a[b"') == {"cmd": "echo {a[b"}


def test_repair_truncated_nested_structure() -> None:
    assert _repair_json('{"edits": [{"old": "x", "new": "y"}, {"old": "z"') == {
        "edits": [{"old": "x", "new": "y"}, {"old": "z"}]
    }


def test_repair_dangling_key_dropped() -> None:
    assert _repair_json('{"path": "/tmp/x", "mo') == {"path": "/tmp/x"}
    assert _repair_json('{"path": "/tmp/x", "mode":') == {"path": "/tmp/x"}


def test_repair_trailing_escape() -> None:
    assert _repair_json('{"path": "C:\\\\tmp\\') == {"path": "C:\\tmp"}


def test_repair_unrecoverable_falls_back_to_empty() -> None:
    assert _repair_json("not json at all") == {}
    assert _repair_json('{"a"') == {}


def test_repair_non_dict_returns_empty() -> None:
    assert _repair_json("[1, 2") == {}
    assert _repair_json('"just a string"') == {}


async def test_truncated_tool_args_repaired_end_to_end() -> None:
    events = [
        ev(
            type="content_block_start",
            index=0,
            content_block=ev(type="tool_use", id="tc_9", name="write", input={}),
        ),
        ev(
            type="content_block_delta",
            index=0,
            delta=ev(type="input_json_delta", partial_json='{"path": "/tmp/a", "content": "hel'),
        ),
        ev(type="content_block_stop", index=0),
        ev(type="message_delta", delta=ev(stop_reason="tool_use"), usage=usage_ns(output_tokens=5)),
        ev(type="message_stop"),
    ]
    provider, _ = make_provider(events)
    out = await provider.complete(make_req())
    assert out.tool_calls == (
        ToolCall(id="tc_9", name="write", args={"path": "/tmp/a", "content": "hel"}),
    )


# --- cache_control placement ----------------------------------------------------------


async def test_cache_control_on_last_section_of_each_stability_group() -> None:
    provider, client = make_provider()
    req = make_req(
        system=(
            PromptSection("identity", "id", Stability.STATIC),
            PromptSection("tools-doc", "td", Stability.STATIC),
            PromptSection("memory", "mem", Stability.SESSION),
            PromptSection("now", "ts", Stability.VOLATILE),
        )
    )
    await provider.complete(req)
    system = client.messages.kwargs["system"]
    assert [b["text"] for b in system] == ["id", "td", "mem", "ts"]
    assert "cache_control" not in system[0]  # static, but not last of its group
    assert system[1]["cache_control"] == {"type": "ephemeral"}  # static group end
    assert system[2]["cache_control"] == {"type": "ephemeral"}  # session group end
    assert "cache_control" not in system[3]  # volatile never gets a breakpoint


async def test_cache_control_on_last_tool_and_last_user_message() -> None:
    provider, client = make_provider()
    tools = (
        ToolSpec("read", "r", {"type": "object"}, Effects.READ_PATH),
        ToolSpec("bash", "b", {"type": "object"}, Effects.EXEC),
    )
    messages = (
        UserMessage("first"),
        AssistantMessage((ToolCall("tc_1", "read", {"path": "x"}),)),
        ToolResultMessage((ToolResult("tc_1", "data"),)),
    )
    await provider.complete(make_req(tools=tools, messages=messages))
    kwargs = client.messages.kwargs
    assert "cache_control" not in kwargs["tools"][0]
    assert kwargs["tools"][1]["cache_control"] == {"type": "ephemeral"}
    api_msgs = kwargs["messages"]
    # last user-role message is the tool_result; its final block is stamped
    assert api_msgs[-1]["role"] == "user"
    assert api_msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # the earlier user message is untouched
    assert api_msgs[0] == {"role": "user", "content": "first"}


async def test_cache_ttl_from_constructor() -> None:
    provider, client = make_provider(cache_ttl="1h")
    await provider.complete(make_req())
    system = client.messages.kwargs["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# --- message conversion ------------------------------------------------------------


async def test_assistant_blocks_and_tool_results_convert() -> None:
    provider, client = make_provider()
    messages = (
        UserMessage("go"),
        AssistantMessage(
            (
                ThinkingBlock("private"),
                TextBlock("on it"),
                ToolCall("tc_1", "read", {"path": "x"}),
            )
        ),
        ToolResultMessage((ToolResult("tc_1", "oops", is_error=True),)),
    )
    await provider.complete(make_req(messages=messages))
    api_msgs = client.messages.kwargs["messages"]
    assert api_msgs[1]["role"] == "assistant"
    # thinking is dropped on replay (no signature in kernel types)
    assert api_msgs[1]["content"] == [
        {"type": "text", "text": "on it"},
        {"type": "tool_use", "id": "tc_1", "name": "read", "input": {"path": "x"}},
    ]
    result_block = dict(api_msgs[2]["content"][0])
    result_block.pop("cache_control", None)
    assert result_block == {
        "type": "tool_result",
        "tool_use_id": "tc_1",
        "content": "oops",
        "is_error": True,
    }


# --- OAuth dispatch -----------------------------------------------------------------


async def test_oauth_system_as_user_injection() -> None:
    provider, client = make_provider(api_key="sk-ant-oat01-token")
    req = make_req(
        system=(
            PromptSection("identity", "you are forge", Stability.STATIC),
            PromptSection("memory", "remember x", Stability.SESSION),
        )
    )
    await provider.complete(req)
    kwargs = client.messages.kwargs
    assert [b["text"] for b in kwargs["system"]] == [OAUTH_IDENTITY]
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    first = kwargs["messages"][0]
    assert first["role"] == "user"
    assert first["content"][0]["text"] == "you are forge\n\nremember x"
    assert first["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["extra_headers"]["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"


async def test_api_key_mode_has_no_oauth_betas_or_injection() -> None:
    provider, client = make_provider()
    await provider.complete(make_req())
    kwargs = client.messages.kwargs
    assert "extra_headers" not in kwargs
    assert kwargs["messages"][0]["content"][0]["text"] == "hi"  # stamped, not injected
    assert kwargs["system"][0]["text"] == "you are forge"


async def test_oauth_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    def record(**kw: Any) -> FakeSDKClient:
        created.append(kw)
        return FakeSDKClient(happy_events())

    monkeypatch.setattr(anthropic, "AsyncAnthropic", record)
    await AnthropicProvider("sk-ant-oat01-token").complete(make_req())
    assert created[0]["auth_token"] == "sk-ant-oat01-token"
    assert created[0]["default_headers"]["x-app"] == "cli"
    await AnthropicProvider("sk-ant-api03-plain").complete(make_req())
    assert created[1] == {"api_key": "sk-ant-api03-plain"}


# --- effort -> thinking mapping -------------------------------------------------------


async def test_effort_maps_to_thinking_budget() -> None:
    provider, client = make_provider()
    await provider.complete(make_req(effort=Effort.HIGH))
    kwargs = client.messages.kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 16000}
    assert kwargs["max_tokens"] > 16000
    assert "interleaved-thinking-2025-05-14" in kwargs["extra_headers"]["anthropic-beta"]


async def test_effort_none_sends_no_thinking() -> None:
    provider, client = make_provider()
    await provider.complete(make_req(effort=Effort.NONE))
    assert "thinking" not in client.messages.kwargs


@pytest.mark.parametrize("model", ["claude-3-5-haiku-20241022", "totally-unknown-model"])
async def test_effort_dropped_when_model_lacks_thinking(model: str) -> None:
    provider, client = make_provider()
    await provider.complete(make_req(model=model, effort=Effort.HIGH))
    kwargs = client.messages.kwargs
    assert "thinking" not in kwargs
    assert "extra_headers" not in kwargs


# --- transient / fatal classification ---------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 529])
async def test_retryable_status_codes_are_transient(code: int) -> None:
    provider, _ = make_provider(error=status_error(code))
    with pytest.raises(TransientProviderError):
        await provider.complete(make_req())


async def test_connection_and_timeout_errors_are_transient() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in (
        anthropic.APIConnectionError(request=request),
        anthropic.APITimeoutError(request=request),
    ):
        provider, _ = make_provider(error=exc)
        with pytest.raises(TransientProviderError):
            await provider.complete(make_req())


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
async def test_non_retryable_status_codes_are_fatal(code: int) -> None:
    provider, _ = make_provider(error=status_error(code))
    with pytest.raises(FatalProviderError):
        await provider.complete(make_req())


async def test_unexpected_error_is_fatal_not_raw() -> None:
    provider, _ = make_provider(error=ValueError("sdk went sideways"))
    with pytest.raises(FatalProviderError, match="sdk went sideways"):
        await provider.complete(make_req())


async def test_no_internal_retry() -> None:
    provider, client = make_provider(error=status_error(429))
    with pytest.raises(TransientProviderError):
        await provider.complete(make_req())
    assert len(client.messages.calls) == 1


# --- info() -------------------------------------------------------------------------


async def test_info_known_model_has_pricing() -> None:
    provider, _ = make_provider()
    info = await provider.info("claude-sonnet-4-5-20250929")
    assert info.id == "claude-sonnet-4-5-20250929"
    assert info.pricing is not None
    assert info.pricing.input_per_mtok == 3.0
    assert info.context_tokens == 200_000
    assert Effort.HIGH in info.efforts


async def test_info_unknown_model_pricing_none() -> None:
    provider, _ = make_provider()
    info = await provider.info("some-future-model")
    assert info.id == "some-future-model"
    assert info.pricing is None  # report tokens, omit cost — never silently wrong
    assert info.efforts == frozenset({Effort.NONE})


# --- conformance ---------------------------------------------------------------------


async def test_provider_conformance_kit() -> None:
    provider, _ = make_provider()
    out = await check_provider_contract(provider, make_req())
    assert out.tool_calls and out.usage.output_tokens == 42
