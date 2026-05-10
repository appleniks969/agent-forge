# Getting started

Install agent-forge, run your first session, and set up the wiki — under 10 minutes.

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

> **Sample transcript:** _(TODO — paste a real capture of one short REPL session here so new users know what to expect)_

For the full slash command reference, see [FAQ → Slash commands](faq.md#slash-command-reference).

## One-shot mode (for scripts and CI)

```bash
agent-forge --prompt "summarise the failing tests and suggest a fix"
```

The agent runs, prints its final answer, and exits. No REPL.

## Set up the wiki (recommended, ~30 seconds)

The wiki is a per-repo knowledge layer that gathers signal from your codebase (commits, PRs, hot files, hand-written notes) and **auto-injects it into every chat turn's system prompt**. The agent shows up to your repo already knowing what changed recently and where the hot files are.

```bash
cd ~/your-repo
agent-forge wiki init     # auto-detects packages/* or src/* → contexts.yaml
agent-forge wiki gather   # pulls repo signal into .agent-forge/raw/
agent-forge               # chat as usual — WIKI section is now in the prompt
```

That's the whole minimum-viable flow. Subsequent `wiki gather` runs are incremental.

The wiki is **optional** — every chat turn works without it. If you skip this step, the agent just won't have the auto-injected repo context until you add it.

For the full wiki workflow (compile, ratchet, compact, maintain), see the [Wiki section in the top-level README](../../README.md#wiki--repository-knowledge).

## What's next

- [Configuration](configuration.md) — pick a model, tune thinking modes, manage memory
- [FAQ](faq.md) — slash command reference, troubleshooting
- **Autonomous mode** — unattended, git-isolated execution with verify-and-PR delivery. See the [Autonomous Mode section in the top-level README](../../README.md#autonomous-mode).
