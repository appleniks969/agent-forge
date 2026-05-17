# Getting started

Install agent-forge, run your first session, and set up the wiki skill — under 10 minutes.

## Prerequisites

| Requirement | Minimum | Check |
|---|---|---|
| Python | 3.12 | `python --version` |
| macOS or Linux | — | — |
| Anthropic API key **or** Claude Code OAuth token | — | see [Set your API key](#set-your-api-key) |

`uv` is installed for you by `install.sh` if missing — you don't need it ahead of time.

## Install

```bash
git clone <repo-url> agent-forge
cd agent-forge
bash install.sh
```

The installer:
1. Installs `uv` if not already present.
2. Runs `uv tool install .` — installs `agent-forge` into an isolated environment and puts the binary on `~/.local/bin`.
3. Adds `~/.local/bin` to your `PATH` in `~/.zshrc` / `~/.bashrc` if needed.
4. Smoke-tests that `agent-forge` is callable.

**Open a new terminal** (or `source ~/.zshrc`), then verify:

```bash
agent-forge --help
```

If you see `command not found`, see [FAQ](faq.md#agent-forge-command-not-found).

## Set your API key

Pick **one**:

```bash
# Option A — Anthropic API key (recommended for teams)
export ANTHROPIC_API_KEY="sk-ant-..."

# Option B — Claude Code OAuth token (personal accounts)
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."
```

Make it permanent by adding the export to `~/.zshrc` or `~/.bashrc`. See [Configuration → Authentication](configuration.md#authentication) for the team-vs-personal tradeoff.

## Your first session

```bash
cd /your/project
agent-forge
```

You'll land in an interactive REPL. Try:

```
> explain this codebase
```

The agent has six built-in tools — `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Find` — sandboxed to the working directory. It will read files, run commands, and stream back its findings.

A few things to try in your first session:

- `/status` — see the current model, session ID, token count, turn count.
- `/model` — switch model mid-session without restarting.
- `Ctrl-C` — interrupt a running agent turn.
- `/remember <text>` — save `<text>` to project `memory.md`. Memory is auto-injected into every future session's system prompt, so the agent picks up project conventions without you re-explaining them.
- `/quit` (or `Ctrl-D`) — exit cleanly.

Here's what a short session looks like end-to-end:

```text
$ cd ~/projects/payments-api
$ agent-forge

  Model: claude-sonnet-4-6 · 1000K ctx · /quit /clear /status /remember /mcp
  cwd:   ~/projects/payments-api  (branch: main)

> what does the retry logic do in webhooks.py?

  Read webhooks.py (lines 1-120)
  Grep "retry" in src/

  The webhook handler retries failed deliveries up to 3 times with
  exponential backoff (1s, 2s, 4s). The retry budget is per-event, not
  per-endpoint — see line 87. Failures after the 3rd attempt are written
  to the dead_letter table.

  │ 12.3K tokens · 2 turns · $0.012

> /remember webhook retries are per-event, max 3, exp backoff 1/2/4s
  saved to .agent-forge/memory.md

> /quit
```

For the full slash command reference, see [FAQ → Slash commands](faq.md#slash-command-reference).

## One-shot mode (for scripts and CI)

```bash
agent-forge --prompt "summarise the failing tests and suggest a fix"
```

The agent runs, prints its final answer, and exits. No REPL.

## Set up the wiki skill (recommended)

The **agent-forge-wiki skill** is a per-repo knowledge layer that gathers
signal from your codebase (commits, PRs, hot files, hand-written notes)
into schema'd bundles, and LLM-compiles narrative cards. The skill ships
in this repo at `.claude/skills/agent-forge-wiki/` and is auto-discovered
by Claude Code.

```bash
cd ~/your-repo

# One-time area detection
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py init

# Pull repo signal (incremental on subsequent runs)
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py gather --since 2026-02-10

# Synthesise narrative cards (LLM call)
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py compile

# Chat — agents discover the skill via SKILL.md
agent-forge
```

The wiki is **optional** — every chat turn works without it.

For the full wiki workflow (compile, compact, maintain) and the skill's
internal architecture, see the
[Wiki section in the top-level README](../../README.md#wiki-skill).

## What's next

- [Configuration](configuration.md) — pick a model, tune thinking modes, manage memory
- [Team setup](team-setup.md) — secrets distribution, shared MCP config, CI usage, recommended defaults
- [FAQ](faq.md) — slash command reference, troubleshooting
