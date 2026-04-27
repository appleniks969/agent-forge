"""agent_forge — minimal coding agent, 7 flat modules."""
from .provider import (
    AssistantMessage, Message, Model, MODELS, DEFAULT_MODEL,
    SystemPromptSection, TextContent, ThinkingContent, ToolCallContent,
    ToolDefinition, ToolResult, ToolResultMessage, TokenUsage, UserMessage,
    ZERO_USAGE, AnthropicProvider,
)
from .tools import Tool, ToolRegistry, default_registry
from .context import (
    ContextWindow, ContextBudget, PressureTier, SectionName, SystemPrompt,
    assess_pressure, ABSOLUTE_P4, ABSOLUTE_P3, ABSOLUTE_AGG,
)
from .session import (
    append_message, append_metadata, load_memory, load_memory_deduped,
    new_id, resume_session, update_memory,
)
from .loop import (
    AgentConfig, AgentResult, AgentEvent, agent_loop, make_config,
    DoneAgentEvent, TurnStartEvent, TextDeltaAgentEvent, ToolResultAgentEvent,
)
from .chat import ChatConfig, run_chat
from .autonomous import AutonomousConfig, AutonomousFlow, FlowResult, run_autonomous

__all__ = [
    "AssistantMessage", "Message", "Model", "MODELS", "DEFAULT_MODEL",
    "SystemPromptSection", "TextContent", "ThinkingContent", "ToolCallContent",
    "ToolDefinition", "ToolResult", "ToolResultMessage", "TokenUsage", "UserMessage",
    "ZERO_USAGE", "AnthropicProvider",
    "Tool", "ToolRegistry", "default_registry",
    "ContextWindow", "ContextBudget", "PressureTier", "SectionName", "SystemPrompt",
    "assess_pressure", "ABSOLUTE_P4", "ABSOLUTE_P3", "ABSOLUTE_AGG",
    "append_message", "append_metadata", "load_memory", "load_memory_deduped",
    "new_id", "resume_session", "update_memory",
    "AgentConfig", "AgentResult", "AgentEvent", "agent_loop", "make_config",
    "DoneAgentEvent", "TurnStartEvent", "TextDeltaAgentEvent", "ToolResultAgentEvent",
    "ChatConfig", "run_chat",
    "AutonomousConfig", "AutonomousFlow", "FlowResult", "run_autonomous",
]
