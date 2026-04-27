# agent-forge

A minimal Python coding agent — interactive REPL and autonomous pipeline backed by Claude.

```
cd /your/project
agent-forge
> explain this codebase
```

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [API Key Setup](#api-key-setup)
4. [Quick Start](#quick-start)
5. [CLI Reference](#cli-reference)
6. [Slash Commands](#slash-commands)
7. [Models](#models)
8. [Thinking Mode](#thinking-mode)
9. [Session Management](#session-management)
10. [Autonomous Mode](#autonomous-mode)
11. [Troubleshooting](#troubleshooting)
12. [For Developers — Extending agent-forge](#for-developers--extending-agent-forge)

---

## Prerequisites

| Requirement | Minimum version | Check |
|---|---|---|
| Python | 3.12 | `python --version` |
| [uv](https://docs.astral.sh/uv/) | any recent | `uv --version` |
| Anthropic API key **or** Claude Code OAuth token | — | see [API Key Setup](#api-key-setup) |

> **uv** is the only installer requirement. The script installs it for you if it is missing.

---

## Installation

### One-liner (recommended)

Clone the repo and run the installer:

```bash
git clone <repo-url> agent-forge
cd agent-forge
bash install.sh
```

The installer:
1. Installs `uv` if not already present.
2. Runs `uv tool install .` — installs `agent-forge` into an isolated env and puts the binary on `~/.local/bin`.
3. Adds `~/.local/bin` to your `PATH` in `~/.zshrc` / `~/.bashrc` if needed.
4. Smoke-tests that `agent-forge` is callable.

After installation, **open a new terminal** (or `source ~/.zshrc`) and you are done.

### Manual install (editable / development)

```bash
cd agent-forge
uv pip install -e .          # installs in the current venv
```

### Verify

```bash
agent-forge --help
```

---

## API Key Setup

agent-forge reads credentials from environment variables. Set **one** of these before running:

```bash
# Option A — Anthropic API key (recommended for teams)
export ANTHROPIC_API_KEY="sk-ant-..."

# Option B — Claude Code OAuth token (personal accounts)
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."
```

**Persistent setup** — add the export to your shell profile so it survives restarts:

```bash
# ~/.zshrc or ~/.bashrc
export ANTHROPIC_API_KEY="sk-ant-..."
```

> **For teams:** provision a single Anthropic Teams / Enterprise API key and distribute it via your organisation's secrets manager (e.g. 1Password, AWS Secrets Manager, Vault). Do **not** share OAuth tokens — they are personal credentials tied to an individual Anthropic account.

---

## Quick Start

```bash
# Navigate to the project you want to work on
cd /your/project

# Start the interactive REPL
agent-forge

# Single-prompt non-interactive mode (great for scripts and CI)
agent-forge --prompt "add docstrings to all public functions in src/"
```

The agent has access to six built-in tools:

| Tool | What it does |
|---|---|
| **Bash** | Runs shell commands (tests, builds, git) |
| **Read** | Reads a file with line numbers; supports offset/limit for large files |
| **Write** | Creates or overwrites a file |
| **Edit** | Targeted find-and-replace within an existing file |
| **Grep** | Searches file contents by regex; uses `rg` when available |
| **Find** | Lists files matching a glob pattern, sorted by modification time |

All tool paths are sandboxed to the working directory — the agent cannot read or write outside it.

---

## CLI Reference

```
agent-forge [OPTIONS]

Options:
  --model <id>          Model to use (default: claude-sonnet-4-6)
                        See --model values in the Models section below.

  --thinking <level>    Thinking budget (default: adaptive)
                        Choices: off | adaptive | low | medium | high

  --cwd <path>          Working directory for all tool calls (default: $PWD)

  --continue            Resume the most recent session for this working directory

  --resume <id>         Resume a specific session by ID (partial ID is fine)

  --verbose             Print context pressure tier changes and memory saves

  --prompt <text>       Run a single prompt non-interactively, then exit

  --help                Show this help and exit
```

### Examples

```bash
# Default interactive REPL in current directory
agent-forge

# Use a faster / cheaper model
agent-forge --model claude-haiku-4-5

# Use the most powerful model with maximum thinking
agent-forge --model claude-opus-4-7 --thinking high

# Disable thinking (faster, lower cost)
agent-forge --thinking off

# Run in a different project directory
agent-forge --cwd ~/projects/myapp

# One-shot: explain then exit (good for CI or scripts)
agent-forge --prompt "summarise the failing tests and suggest a fix"

# Resume last session
agent-forge --continue

# Resume a specific session
agent-forge --resume a3f9
```

---

## Slash Commands

Available inside the interactive REPL:

| Command | Effect |
|---|---|
| `/quit` or `/exit` or `/q` | Exit the REPL; saves session learnings to `memory.md` |
| `/clear` | Clear the current conversation and context window |
| `/status` | Show session ID, current model, token count, and turn count |
| `/model` | Switch model interactively without restarting |

> **Tip:** Press `Ctrl-C` to interrupt a running agent turn. Press `Ctrl-D` (or type `/quit`) to exit cleanly.

---

## Models

| Model ID | Context window | Best for | Relative cost |
|---|---|---|---|
| `claude-sonnet-4-6` | 1 M tokens | Default — fast, capable, good reasoning | $$ |
| `claude-sonnet-4-5` | 200 K tokens | Longer sessions on older model | $$ |
| `claude-haiku-4-5` | 200 K tokens | Fast, cheap, simple tasks | $ |
| `claude-opus-4-7` | 1 M tokens | Complex reasoning, large codebases | $$$$ |

Switch at startup:

```bash
agent-forge --model claude-haiku-4-5
```

Or switch mid-session with `/model`.

---

## Thinking Mode

Claude's extended thinking lets the model reason through hard problems before answering.

| Level | Behaviour | When to use |
|---|---|---|
| `adaptive` (default) | Model decides when to think and for how long | Best general setting |
| `off` | No thinking — fastest and cheapest | Simple tasks, CI pipelines |
| `low` | Up to ~1 K thinking tokens | Light reasoning |
| `medium` | Up to ~4 K thinking tokens | Standard debugging / refactoring |
| `high` | Up to ~16 K thinking tokens | Architecture, complex bug hunts |

```bash
agent-forge --thinking off     # fastest
agent-forge --thinking high    # most thorough
```

---

## Session Management

Every interactive session is automatically saved as a JSONL log under `~/.agent-forge/sessions/`.

### Resume a session

```bash
# Resume the last session for the current project directory
agent-forge --continue

# Resume a specific session (use the first few chars of the session ID)
agent-forge --resume a3f9
```

### Memory

At the end of each session (on `/quit` or `Ctrl-D`) agent-forge extracts learnings from the conversation and writes them to a `memory.md` file in your project directory. On the next session that file is injected into the system prompt so the agent remembers project-specific preferences.

You can safely delete `memory.md` at any time to reset project memory.

---

## Autonomous Mode

For unattended, git-isolated execution — the agent works in a throwaway git worktree and never touches your main branch.

### Prerequisites for autonomous mode

- The repo must have a **clean working tree** (no uncommitted changes).
- You must be on a **named branch** (not detached HEAD).
- For PR delivery: the [GitHub CLI (`gh`)](https://cli.github.com/) must be installed and authenticated.

### Python API

```python
import asyncio
from agent_forge import run_autonomous, AutonomousConfig

result = asyncio.run(run_autonomous(AutonomousConfig(
    task="Add type annotations to all functions in src/utils.py",
    api_key="sk-ant-...",           # or read from os.environ
    repo_path="/your/project",      # must be a git repo
    verify_commands=["pytest -x"],  # runs after the agent finishes; all must pass
    delivery="pr",                  # "pr" | "merge" | "output" | "none"
    max_turns=50,
    thinking="off",
)))

print(result.success)   # True / False
print(result.output)    # PR URL, merge message, or agent's final text
print(result.error)     # set if success=False
```

### `AutonomousConfig` fields

| Field | Type | Default | Description |
|---|---|---|---|
| `task` | `str` | required | The task description sent to the agent |
| `api_key` | `str` | required | `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` value |
| `model` | `Model` | `claude-sonnet-4-6` | Model to use |
| `repo_path` | `str` | `"."` | Path to the git repository root |
| `branch_prefix` | `str` | `"agent-forge"` | New branch will be `agent-forge/<timestamp>` |
| `verify_commands` | `list[str]` | `[]` | Shell commands that must all exit 0 before delivery |
| `delivery` | `str` | `"pr"` | `"pr"` (push + open PR) · `"merge"` (merge to current branch) · `"output"` (return text only) · `"none"` (leave worktree in place) |
| `max_turns` | `int` | `50` | Hard cap on agent turns |
| `thinking` | `str` | `"off"` | Same levels as CLI |
| `verbose` | `bool` | `False` | Print tool call events |

### Flow states

```
GATING → ISOLATED → EXECUTING → VERIFYING → DELIVERING → DONE
                                                ↓ (any state on error)
                                             FAILED
```

The worktree is always cleaned up (via `try/finally`) on both success and failure.

---

## Troubleshooting

### `Error: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY`

You have not set an API key. See [API Key Setup](#api-key-setup).

### `agent-forge: command not found`

The binary is on `~/.local/bin` but that path is not in your `PATH`. Fix:

```bash
export PATH="$HOME/.local/bin:$PATH"
# Make permanent:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

### `Unknown model: '...'`

Run `agent-forge --model <id>` with one of the supported IDs listed in [Models](#models).

### `Working tree has uncommitted changes` (autonomous mode)

Commit or stash your changes before running autonomous mode:

```bash
git stash
# run autonomous task
git stash pop
```

### The agent hits `max_turns`

The agent stopped after reaching the turn limit without finishing. Either increase `--max-turns` (or `max_turns` in `AutonomousConfig`) or break the task into smaller pieces.

### Tool output is truncated

Tool results are capped at 50 KB. For `Read`, use `offset` and `limit` to page through large files. For `Grep`, narrow the glob or pattern.

### High API costs

- Use `--model claude-haiku-4-5` for simple tasks.
- Use `--thinking off` when reasoning is not needed.
- Use `--max-turns 10` (or lower) to prevent runaway loops.
- Prompt cost is reduced automatically on repeated turns because the system prompt is cached.

---

## For Developers — Extending agent-forge

### Project layout

```
agent_forge/
  provider.py      ← message types, model catalog, Anthropic streaming adapter
  tools.py         ← Tool protocol, ToolRegistry, 6 built-in tools
  context.py       ← ContextWindow, pressure tiers, system prompt builder
  session.py       ← JSONL session log, resume, memory.md read/write
  loop.py          ← agent_loop() async generator, AgentConfig/AgentResult
  prompts.py       ← system prompt sections, AGENTS.md loader, repo map
  renderer.py      ← ANSI helpers, event renderer, turn footer
  chat.py          ← interactive REPL, CLI entry point (composition root)
  autonomous.py    ← AutonomousFlow state machine (composition root)
eval.py            ← evaluation / test suite (run with: python eval.py)
pyproject.toml     ← package metadata, dependencies, entry point
install.sh         ← one-step installer
AGENTS.md          ← architecture guide for contributors and AI agents
```

### Add a custom tool

1. Implement a class following the `BashTool` pattern in `tools.py`:

```python
class MyTool:
    name = "MyTool"
    description = "One-line description of what the tool does."
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input value"},
        },
        "required": ["input"],
    }

    def definition(self):
        from agent_forge import ToolDefinition
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal=None):
        from agent_forge import ToolResult
        value = args.get("input", "")
        return ToolResult(content=f"Result: {value}")
```

2. Register it:

```python
from agent_forge import default_registry
registry = default_registry()
registry.register(MyTool())
```

3. Wire it into the loop:

```python
from agent_forge import make_config, agent_loop, UserMessage

cfg = make_config(
    model=..., api_key=..., system_prompt=...,
    tool_registry=registry, cwd=".",
)
async for event in agent_loop(cfg, [UserMessage(content="use MyTool")]):
    ...
```

### Add a new model

Add an entry to `MODELS` in `provider.py`:

```python
MODELS["claude-new-model"] = Model(
    id="claude-new-model",
    context_window=200_000,
    max_tokens=64_000,
    reasoning=True,
    cost=ModelCost(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75),
)
```

### Run the test suite

```bash
cd agent_forge

# Unit tests — no API key needed (evals 1–8)
python eval.py

# Full suite including live API calls (evals 9–10)
ANTHROPIC_API_KEY=sk-ant-... python eval.py
```

### Dependency rules

Modules form a strict layered hierarchy. Lower layers must never import from higher ones:

```
provider  →  tools / context / session  →  loop  →  prompts / renderer  →  chat / autonomous
```

See `AGENTS.md` for the full architecture reference, concept index, and change-impact map.
