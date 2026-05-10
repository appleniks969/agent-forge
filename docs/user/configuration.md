# Configuration

How to set credentials, pick a model, tune the thinking budget, and manage project memory.

## Authentication

agent-forge reads credentials from environment variables. Set **one** of these:

```bash
# Option A — Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Option B — Claude Code OAuth token
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."
```

Make it permanent by adding the export to `~/.zshrc` or `~/.bashrc`.

### Which one should I use?

| Use case | Pick |
|---|---|
| Personal account, single user | OAuth token |
| Team / organisation | Anthropic API key, distributed via your secrets manager (1Password, AWS Secrets Manager, Vault, etc.) |

> **Don't share OAuth tokens.** They are personal credentials tied to an individual Anthropic account.

If neither variable is set you'll see:

```
Error: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
```

## Models

| Model ID | Context window | Best for | Relative cost |
|---|---|---|---|
| `claude-sonnet-4-6` | 1 M tokens | **Default** — fast, capable, good reasoning | $$ |
| `claude-sonnet-4-5` | 200 K tokens | Longer sessions on the older model | $$ |
| `claude-haiku-4-5` | 200 K tokens | Fast, cheap, simple tasks | $ |
| `claude-opus-4-7` | 1 M tokens | Complex reasoning, large codebases | $$$$ |

Pick at startup:

```bash
agent-forge --model claude-haiku-4-5
```

Switch mid-session with `/model`.

To add a new model entry, see [Add a new model](../../README.md#add-a-new-model) in the top-level README.

## Thinking modes

Claude's extended thinking lets the model reason through hard problems before answering.

| Level | Budget | When to use |
|---|---|---|
| `medium` *(default)* | ~4 K thinking tokens | Standard debugging / refactoring — best quality/cost tradeoff in our eval matrix |
| `off` | none | Simple tasks, CI pipelines — fastest and cheapest |
| `low` | ~1 K thinking tokens | Light reasoning |
| `adaptive` | model self-budgets (Sonnet 4.6 / Opus 4.6+) | Experimental — observed to under-allocate on hard tasks |
| `high` | ~16 K thinking tokens | Architecture, complex bug hunts |

```bash
agent-forge --thinking off     # fastest
agent-forge --thinking high    # most thorough
```

## Memory

agent-forge keeps a `memory.md` file of project-specific preferences and conventions. Its contents are injected into the system prompt on every session, so the agent picks up where you left off.

| Question | Answer |
|---|---|
| Where does it live? | **Project memory:** `<cwd>/.agent-forge/memory.md` · **Global memory:** `~/.agent-forge/memory.md` (merged with project memory at load time) |
| How is it written? | **Explicitly only**, via the `/remember <text>` slash command in the REPL. There is no automatic save on exit. |
| How do I edit it? | It's plain markdown — open in any editor. |
| How do I reset? | Delete the file: `rm .agent-forge/memory.md` (project) or `rm ~/.agent-forge/memory.md` (global). |
| What's the size cap? | ~2 K tokens per file. Older entries are dropped when the cap is hit. Duplicate entries (60-char prefix match) are deduplicated automatically. |

### Session ratchet (separate from memory)

If you want the agent to distil per-session insights, opt into the **ratchet**: it writes a structured note to `.agent-forge/raw/notes/session/<sid>.md` and costs one LLM call per session.

- `agent-forge --ratchet` — auto-runs on clean exit (`/quit`, `/exit`, `/q`, `Ctrl-D`). Not run on `Ctrl-C` or crash.
- `/ratchet` — run on demand mid-session.

Ratchet output feeds the wiki (the `present` stage reads `raw/notes/session/`), not the system-prompt memory section.

## Sessions

Every interactive session is auto-saved as a JSONL log under `~/.agent-forge/sessions/`.

```bash
agent-forge --continue        # resume the last session for this directory
agent-forge --resume a3f9     # resume a specific session by ID prefix
```

## CLI flags reference

| Flag | Default | Description |
|---|---|---|
| `--model <id>` | `claude-sonnet-4-6` | Model to use |
| `--thinking <level>` | `medium` | `off` · `adaptive` · `low` · `medium` · `high` |
| `--cwd <path>` | `$PWD` | Working directory for all tool calls |
| `--continue` | — | Resume the most recent session for this `--cwd` |
| `--resume <id>` | — | Resume a specific session by ID prefix |
| `--prompt <text>` | — | Run a single prompt non-interactively, then exit |
| `--ratchet` | off | On clean exit, distil the session into `.agent-forge/raw/notes/session/<sid>.md` via one LLM call |
| `--verbose` | off | Print context-pressure tier changes |
| `--debug-stream` | off | Log raw provider stream events with timestamps to stderr (diagnostic) |
| `--help` | — | Show help and exit |

## Wiki configuration

The wiki reads `.agent-forge/contexts.yaml` for area definitions:

```yaml
areas:
  payments:
    paths:
      - "src/payments/**"
      - "src/billing/**"
  auth:
    paths:
      - "src/auth/**"
```

Without `contexts.yaml`, the wiki still works — hot files just appear as one flat list instead of grouped by area. `agent-forge wiki init` generates a starter file by auto-detecting `packages/*/`, `apps/*/`, `services/*/`, `src/*/`, or top-level dirs.

All wiki state lives under `.agent-forge/` in the target repo. Add it to `.gitignore` unless you want to commit curated knowledge for your team.

For the full wiki workflow, see the [Wiki section in the top-level README](../../README.md#wiki--repository-knowledge).
