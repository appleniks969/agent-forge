# agent-forge documentation

A minimal Python coding agent — interactive REPL backed by Claude, with
six sandboxed built-in tools (Bash · Read · Write · Edit · Grep · Find)
and optional Model Context Protocol support.

## For users

Start here if you want to **use** agent-forge.

| If you want to… | Read |
|---|---|
| Install and run your first session | [user/getting-started.md](user/getting-started.md) |
| Configure auth, models, thinking modes, memory | [user/configuration.md](user/configuration.md) |
| Roll it out to a team (shared secrets, MCP, CI) | [user/team-setup.md](user/team-setup.md) |
| Look up slash commands or troubleshoot | [user/faq.md](user/faq.md) |
| Connect MCP servers (filesystem, GitHub, Postgres, …) | [user/mcp.md](user/mcp.md) |

## For contributors

- [AGENTS.md](../AGENTS.md) — codebase guide for contributors and AI assistants. Module dependency order, concept index, change-impact map, and the policies you must preserve when modifying the loop, the provider, or the context window.

## Single source of truth

Each topic lives in exactly one place. If you change one of these in
code, also update the listed page:

| Surface | Doc | Code |
|---|---|---|
| CLI flags | [user/configuration.md](user/configuration.md) | `agent_forge/chat.py:_parse_args()` |
| Slash commands | [user/faq.md](user/faq.md) | `agent_forge/chat.py:run_chat()` |
| MCP TOML schema & `/mcp` subcommands | [user/mcp.md](user/mcp.md) | `agent_forge/mcp.py`, `agent_forge/chat.py:_handle_mcp_command()` |
