# FAQ & troubleshooting

## Slash command reference

Available inside the interactive REPL:

| Command | Effect |
|---|---|
| `/quit` · `/exit` · `/q` | Exit the REPL cleanly. (With `--ratchet`, also runs the session ratchet on the way out.) |
| `/clear` | Clear the current conversation and context window |
| `/status` | Show session ID, current model, token count, turn count |
| `/model` | Switch model interactively without restarting |
| `/remember <text>` | Save `<text>` to project `memory.md` so it persists across sessions |
| `/sessions` | List recent sessions for the current working directory |
| `/resume <n\|id>` | Switch to another session by index (from `/sessions`) or ID prefix |

> **Tip:** `Ctrl-C` interrupts a running agent turn. `Ctrl-D` (or `/quit`) exits cleanly.

---

## Troubleshooting

### `agent-forge: command not found`

The binary is on `~/.local/bin` but that path isn't on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

### `Error: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY`

You haven't set a credential. See [Configuration → Authentication](configuration.md#authentication).

### `Unknown model: '...'`

Use one of the IDs from [Configuration → Models](configuration.md#models). Check available models with `agent-forge --help`.

### I don't have Python 3.12

Check your version:

```bash
python --version
python3 --version
```

If you're below 3.12, install via [`pyenv`](https://github.com/pyenv/pyenv):

```bash
pyenv install 3.12
pyenv global 3.12
```

Or use your OS package manager (`brew install python@3.12` on macOS).

### I don't have an Anthropic API key

Two options:

- **API key** — get one at [console.anthropic.com](https://console.anthropic.com/). Required for team / org use.
- **OAuth token** — if you have a personal Claude account, use a Claude Code OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`).

See [Configuration → Authentication](configuration.md#authentication) for the tradeoff.

### Sessions feel slow

Pick a faster combination:

- `--model claude-haiku-4-5` for simple tasks
- `--thinking off` when reasoning isn't needed
- The system prompt is cached automatically, so the second turn onwards is much faster

For the cost/quality tradeoff, see [Configuration → Models](configuration.md#models) and [Thinking modes](configuration.md#thinking-modes).

### How do I reset memory?

Memory is only written via the `/remember <text>` slash command — it doesn't auto-save on exit. To reset, delete the file:

```bash
rm .agent-forge/memory.md       # project memory
rm ~/.agent-forge/memory.md     # global memory
```

For wiki-skill state, delete `.agent-forge/raw/` (loses gathered signal) or the whole `.agent-forge/` (full reset). See [Configuration → Memory](configuration.md#memory).

### How do I switch projects?

`cd` to the new project and run `agent-forge` again, or use `--cwd`:

```bash
agent-forge --cwd ~/projects/other-app
```

`memory.md`, sessions, and the wiki-skill state under `.agent-forge/` are **per-project** — each working directory has its own state.

### Can I use agent-forge on a private repo?

Yes. Tool calls are sandboxed to the working directory. The agent sends conversation contents (including any file contents it reads) to the Anthropic API — review your organisation's data-handling policy before using it on sensitive code.

### The agent did something I didn't want

- `Ctrl-C` interrupts the current turn immediately.
- All tool paths are sandboxed to `--cwd` — the agent can't read or write outside it.
- `Edit` and `Write` overwrite files without confirmation. Commit your work before risky tasks, or use [autonomous mode](../../README.md#autonomous-mode), which works in a throwaway git worktree.

### Tool output is truncated

Tool results are capped at 50 KB. For `Read`, use `offset` and `limit` to page through large files. For `Grep`, narrow the glob or pattern.

### High API costs

- `--model claude-haiku-4-5` for simple tasks
- `--thinking off` when reasoning isn't needed
- In autonomous mode (Python API), set `AutonomousConfig(max_turns=10)` (or lower) to prevent runaway loops
- The system prompt is cached automatically across turns

### `Working tree has uncommitted changes` (autonomous mode)

Autonomous mode requires a clean working tree. Commit or stash:

```bash
git stash
# run autonomous task
git stash pop
```

### The agent hits `max_turns` (autonomous mode)

It reached the turn limit without finishing. Either raise `max_turns` in `AutonomousConfig` (default: 100) or break the task into smaller pieces.
