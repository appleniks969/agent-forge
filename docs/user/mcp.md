# MCP — external tool servers

agent-forge speaks the **[Model Context Protocol](https://modelcontextprotocol.io)**.
Drop in a config file and the agent gains every tool that the MCP server
exposes — filesystem, GitHub, Postgres, web fetch, your in-house API,
anything with an MCP adapter.

Nothing here is required. agent-forge ships six built-in tools (Bash,
Read, Write, Edit, Grep, Find) and works fine without MCP. The protocol
is opt-in.

---

## Install

The MCP support is an optional extra:

```bash
uv pip install -e ".[mcp]"
# or with pip:
pip install agent-forge[mcp]
```

Verify:

```bash
python -c "from agent_forge.mcp import MCPClient; print('ok')"
```

If you don't install the extra, `import agent_forge` still works — but
attempting to connect to an MCP server will fail with a clear "install
the [mcp] extra" message.

---

## Configure servers

Two ways: a TOML file (persistent) or CLI flags (ad-hoc).

### Option A — `mcp.toml`

agent-forge reads from two locations, project overrides global by name:

| File | Scope |
|---|---|
| `~/.agent-forge/mcp.toml` | Global — applies in every directory |
| `<cwd>/.agent-forge/mcp.toml` | Project — applies in this directory only; commit if your team should share |

Schema:

```toml
# Filesystem access scoped to specific directories
[servers.fs]
command = "mcp-server-filesystem"
args    = ["/home/me/projects", "/tmp"]

# GitHub repo access
[servers.gh]
command = "mcp-server-github"
env     = { GITHUB_TOKEN = "ghp_xxx" }

# Postgres queries
[servers.db]
command = "mcp-server-postgres"
args    = ["postgresql://user@host:5432/mydb"]
enabled = true        # optional, defaults true; set false to skip
```

Field reference:

| Field | Type | Required | Description |
|---|---|---|---|
| `command` | string | yes | The MCP server executable (must be on `PATH`) |
| `args` | `[string]` | no | Arguments passed to the server |
| `env` | `{string=string}` | no | Extra env vars set in the server process |
| `enabled` | bool | no, default `true` | Skip the server entirely if `false` |

Malformed TOML or a missing `command` is **logged and skipped** — the
REPL never crashes on startup because of one bad server.

### Option B — `--mcp-server` CLI flag

For ad-hoc / one-off runs, no file edits needed:

```bash
agent-forge --mcp-server "fs=mcp-server-filesystem /tmp"
agent-forge --mcp-server "gh=mcp-server-github" \
            --mcp-server "db=psql 'select * from t'"
```

Format: `name=command [args…]`. Args are shell-tokenised, so quotes
survive.

CLI flags **override** file configs with the same name (last write wins).

---

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `--mcp` | on | Load servers from `mcp.toml` (the default). Mostly useful for explicit-ness in scripts. |
| `--no-mcp` | — | Skip the file loader. `--mcp-server` flags still work. |
| `--mcp-server SPEC` | — | Add one server inline. Repeatable. |

There's no command-line listing of servers — see `/mcp` inside the REPL.

---

## Slash commands

Inside the REPL:

| Command | Effect |
|---|---|
| `/mcp` | Show server status table (connected / failed / closed) |
| `/mcp tools` | List every MCP tool currently registered, grouped by server |
| `/mcp reconnect` | Reconnect every server (after editing `mcp.toml` for example) |
| `/mcp reconnect <name>` | Reconnect just one named server |

Example:

```
> /mcp
MCP servers:
  fs       connected  6 tools
  gh       connected  12 tools
  db       failed     0 tools  (FileNotFoundError: no such executable)

> /mcp tools
MCP tools:

Server: fs
- fs__read_file: Read a file from an allowed directory
- fs__write_file: Write a file to an allowed directory
- fs__list_directory: List entries in a directory
- …

Server: gh
- gh__list_repos: List repositories
- gh__create_issue: Create an issue
- …

> /mcp reconnect gh
[mcp] reconnecting gh…
[mcp] gh: connected
```

After `/mcp reconnect`, the MCP tools section in the system prompt is
re-rendered (the agent's cache for stable sections like guidelines and
built-in tools is preserved, so this is cheap).

---

## Tool names

Every MCP tool is **namespaced** as `{server}__{tool}`. If you have two
servers that both expose a tool called `read_file`, they don't
collide:

```
fs__read_file
sandbox__read_file
```

The agent sees the namespaced name and can refer to it directly. In
the prompt's MCP-tools section, tools are grouped under their server
heading so the LLM has the mental model "server `fs` exposes these
tools".

---

## Optional safety: MCPGuardHook

For non-interactive or scripted runs, you can opt in to `MCPGuardHook`,
which **blocks** any MCP tool whose name contains a destructive verb:

```
delete · remove · rm · drop · destroy · truncate · purge · kill ·
terminate · wipe · shutdown · force_*
```

A call like `gh__delete_repo` or `db__drop_table` is refused with a
synthesised tool-error result so the LLM sees why and can adapt.

Wire it via `AgentRuntime(hooks=...)`:

```python
from agent_forge import AgentRuntime, MCPGuardHook
from agent_forge.guards import _CompositeHook

# Allow a trusted server entirely:
hook = MCPGuardHook(allow_servers=("my_trusted_server",))

# Add custom verbs to the blocklist:
hook = MCPGuardHook(extra_verbs=("yeet", "obliterate"))
```

`MCPGuardHook` only inspects MCP-namespaced names; non-MCP tool calls
(Bash, Read, etc.) pass through. Combine with `BashGuardHook` /
`PathGuardHook` (also in `agent_forge.guards`) via `_CompositeHook` for
defence in depth.

---

## Lifecycle

- **Startup.** agent-forge reads configs, spawns each server's stdio
  child, runs the MCP `initialize` handshake, lists tools, and
  registers them under their namespace.
- **Mid-session.** Tools stay registered until you `/mcp reconnect` or
  exit. The system prompt's MCP tools section is cached for the
  session.
- **Shutdown.** Exiting the REPL (`/quit` or `Ctrl-D`) closes every
  server cleanly via `AgentRuntime.aclose()`. The runtime is an async
  context manager — programmatic users should `async with await
  build_runtime_with_mcp(...)` to get the same guarantee.

A failing server doesn't stop agent-forge from starting. The status is
visible via `/mcp`; reconnect once the underlying issue is fixed.

---

## Programmatic use

When using agent-forge as a library (no CLI), drive the factory directly:

```python
from agent_forge import (
    build_runtime_with_mcp, ChatConfig, MCPServerConfig,
    UserMessage, default_registry,
)
from agent_forge.prompts import build_chat_prompt_async

cfg = ChatConfig(api_key=..., cwd=".")
tool_registry = default_registry()
system_prompt = await build_chat_prompt_async(cfg, tool_registry)

mcp_servers = [
    MCPServerConfig(name="fs", command="mcp-server-filesystem", args=("/tmp",)),
]

async with await build_runtime_with_mcp(
    model=cfg.model, system_prompt=system_prompt,
    tool_registry=tool_registry, cwd=cfg.cwd,
    mcp_configs=mcp_servers,
    api_key=cfg.api_key,
) as runtime:
    result = await runtime.run_turn(UserMessage(content="list /tmp"))
    print(result.text)
```

You can also call the loaders directly:

```python
from agent_forge import load_mcp_configs, parse_mcp_server_spec

configs = load_mcp_configs(cwd=".")
adhoc = parse_mcp_server_spec("fs=mcp-server-filesystem /tmp")
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `[mcp] foo: failed (FileNotFoundError)` | `command` isn't on `PATH`. Check `which <your-command>`. |
| Server connects but exposes no tools | Server config is wrong (e.g. missing API key). Run the server manually to inspect its stderr. |
| `/mcp reconnect` says connected but tools didn't refresh | A bug — please file an issue. The hot-reload path calls `tool_registry.replace_mcp_tools(mgr.tools())` and `invalidate_session()` so this should always converge. |
| Agent doesn't seem to know about the new MCP tool | Run `/mcp tools` to confirm registration. If listed there but not invoked, check that the description is clear enough — the LLM picks tools from the description text. |
| Startup is slow | Each server's `initialize` round-trip is on the critical path. Disable unused servers with `enabled = false` in `mcp.toml`. |
| `import agent_forge` fails | Almost certainly unrelated to MCP — MCP is lazy-imported only inside `MCPClient.connect()`. Confirm with `python -c "import agent_forge"`. |

---

## See also

- [Configuration](configuration.md) — auth, models, thinking, memory
- [Team setup](team-setup.md) — sharing `mcp.toml` across your team safely
- [FAQ](faq.md) — slash commands, troubleshooting
