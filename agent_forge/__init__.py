"""agent_forge — minimal coding agent.

Module layout (leaf → root):
  messages.py           ← shared value types (Messages, TokenUsage, …)
  models.py             ← Model catalog + ModelCost
  provider.py           ← LLMProvider Protocol + StreamEvent union
  anthropic_provider.py ← AnthropicProvider adapter (only file that knows the SDK)
  tools.py              ← Tool Protocol + 6 built-ins
  context.py            ← ContextWindow aggregate + pressure tiers
  system_prompt.py      ← SystemPrompt + SectionName (ordered sections)
  session.py            ← JSONL session log + memory.md
  loop.py               ← agent_loop() + AgentEvent + Hooks
  guards.py             ← reusable safety hooks (Bash/Path/MCP guards + _CompositeHook)
  prompts.py            ← system-prompt builder
  renderer.py           ← ANSI/Rich event renderer
  chat.py               ← interactive REPL (composition root)
"""
from .messages import (
    AssistantMessage, ImageContent, Message,
    SystemPromptSection, TextContent, ThinkingContent, ToolCallContent,
    ToolDefinition, ToolResult, ToolResultMessage, TokenUsage, UserMessage,
    ZERO_USAGE,
)
from .models import DEFAULT_MODEL, MODELS, Model, ModelCost
from .provider import LLMProvider
from .tools import Tool, ToolRegistry, default_registry, sanitize_exception

# AnthropicProvider import is best-effort: the SDK is the default but
# architecturally optional (a different LLMProvider can be plugged in).
# A missing SDK should not break `import agent_forge`; only attempts to
# *use* AnthropicProvider should fail.
try:
    from .anthropic_provider import AnthropicProvider
except ImportError:  # pragma: no cover - SDK missing in minimal installs
    AnthropicProvider = None  # type: ignore[assignment,misc]
from .context import (
    ContextWindow, ContextBudget, PressureTier,
    assess_pressure, ABSOLUTE_P4, ABSOLUTE_P3, ABSOLUTE_AGG,
)
from .system_prompt import SectionName, SystemPrompt
from .session import (
    Redactor, append_message, append_metadata, load_memory, load_memory_deduped,
    new_id, redact_secrets, resume_session, update_memory,
)
from .loop import (
    AgentConfig, AgentResult, AgentEvent, agent_loop, make_config,
    DoneAgentEvent, TurnStartEvent, TextDeltaAgentEvent, ToolResultAgentEvent,
    Hooks, NoopHooks, HookDecision,
)
from .hooks import AuditHook
from .runtime import AgentRuntime, build_runtime_with_mcp
# MCP integration — Phases G & H. The mcp.py module itself imports the
# optional `mcp` SDK lazily inside MCPClient.connect(), so this top-level
# import is safe regardless of whether the [mcp] extra is installed.
from .mcp import (
    MCPClient, MCPManager, MCPServerConfig, MCPServerStatus, MCPSession,
    MCPTool, MCPToolDescriptor, load_mcp_configs, namespaced_tool_name,
    parse_mcp_server_spec, unpack_namespaced_name,
)
from .guards import BashGuardHook, MCPGuardHook, PathGuardHook
from .chat import ChatConfig, run_chat

__all__ = [
    "AssistantMessage", "ImageContent", "Message",
    "SystemPromptSection", "TextContent", "ThinkingContent", "ToolCallContent",
    "ToolDefinition", "ToolResult", "ToolResultMessage", "TokenUsage", "UserMessage",
    "ZERO_USAGE",
    "DEFAULT_MODEL", "MODELS", "Model", "ModelCost",
    "LLMProvider", "AnthropicProvider",
    "Tool", "ToolRegistry", "default_registry", "sanitize_exception",
    "ContextWindow", "ContextBudget", "PressureTier",
    "assess_pressure", "ABSOLUTE_P4", "ABSOLUTE_P3", "ABSOLUTE_AGG",
    "SectionName", "SystemPrompt",
    "append_message", "append_metadata", "load_memory", "load_memory_deduped",
    "new_id", "redact_secrets", "Redactor", "resume_session", "update_memory",
    "AgentConfig", "AgentResult", "AgentEvent", "agent_loop", "make_config",
    "DoneAgentEvent", "TurnStartEvent", "TextDeltaAgentEvent", "ToolResultAgentEvent",
    "Hooks", "NoopHooks", "HookDecision", "AuditHook",
    "AgentRuntime", "build_runtime_with_mcp",
    "MCPClient", "MCPManager", "MCPServerConfig", "MCPServerStatus", "MCPSession",
    "MCPTool", "MCPToolDescriptor", "load_mcp_configs", "namespaced_tool_name",
    "parse_mcp_server_spec", "unpack_namespaced_name",
    "BashGuardHook", "MCPGuardHook", "PathGuardHook",
    "ChatConfig", "run_chat",
]
