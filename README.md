# agent-forge

A minimal Python coding agent — interactive REPL and autonomous pipeline backed by Claude.

```
cd /your/project
agent-forge
> explain this codebase
```

---

## Install

Requires Python 3.12+ on macOS or Linux.

```bash
git clone <repo-url> agent-forge
cd agent-forge
bash install.sh
```

The installer installs `uv` if missing, runs `uv tool install .`, puts the `agent-forge` binary on `~/.local/bin`, and adds that path to your shell rc if needed. Open a new terminal (or `source ~/.zshrc`) and verify:

```bash
agent-forge --help
```

## Set your API key

Pick **one**:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."         # team / org use
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."  # personal account
```

Make it permanent in `~/.zshrc` or `~/.bashrc`.

## Quick start

```bash
cd /your/project
agent-forge                                   # interactive REPL
agent-forge --prompt "fix the failing tests"  # one-shot, exits when done
```

Optional but recommended — set up the per-repo wiki (~30 seconds):

```bash
agent-forge wiki init     # auto-detect areas → contexts.yaml
agent-forge wiki gather   # pull repo signal into .agent-forge/raw/
agent-forge               # WIKI section is now auto-injected each turn
```

See the [Wiki](#wiki--repository-knowledge) section below for the full workflow.

---

## Documentation

| If you want to… | Read |
|---|---|
| Install and run your first session | [docs/user/getting-started.md](docs/user/getting-started.md) |
| Configure auth, models, thinking modes, memory | [docs/user/configuration.md](docs/user/configuration.md) |
| Look up slash commands or troubleshoot | [docs/user/faq.md](docs/user/faq.md) |
| Understand the architecture or modify the codebase | [AGENTS.md](AGENTS.md) |

The agent has six built-in tools — `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Find` — sandboxed to the working directory.

---

## Wiki — Repository Knowledge

A per-repo knowledge system that compounds over time. agent-forge gathers
signal from your codebase (commits, PRs, hotspots, code markers, hand-written
notes), optionally synthesises it via an LLM, and **auto-injects it into the
system prompt** on every chat turn. The agent shows up to a new repo already
knowing where the hot files are, what the recent bug fixes were, and what you
wrote down last week.

All state lives under `.agent-forge/` in the target repo (gitignore it).
The wiki subsystem is **optional** — every chat turn works without it; if
`.agent-forge/raw/` is empty, no WIKI section is added to the prompt.

### Minimum viable usage (~30 seconds)

```bash
cd ~/your-repo
agent-forge wiki init         # auto-detect packages/* or src/* → contexts.yaml
agent-forge wiki gather       # pull repo signal into .agent-forge/raw/
agent-forge                   # chat as usual — WIKI section is auto-injected
```

That's the whole flow for "give me value today." Subsequent `wiki gather`
runs are incremental (the cursor in `.agent-forge/raw/cache/.cursor` advances).

`wiki init` inspects the repo for `packages/*/`, `apps/*/`, `services/*/`,
`src/*/`, or top-level dirs and writes a starter `contexts.yaml`. Edit it
freely — paths use glob syntax (`**` matches recursively):

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

Without `contexts.yaml`, everything still works — hot files just appear as
one flat list instead of grouped by area.

### Optional: drop hand-written notes anytime

```bash
echo "# Why webhooks retry 3x not 5x" > .agent-forge/raw/notes/webhook-retries.md
```

The next gather (or chat session — `present` reads notes directly) picks them up.

### The seven stages

Each is a peer subpackage of `agent_forge/wiki/` with a uniform shape
(`__init__.py` + `runner.py`). LLM-using stages are explicit CLI verbs;
nothing happens behind your back unless you opt in with `--ratchet`.

| Stage | Trigger | LLM? | Output |
|---|---|---|---|
| **init** | `agent-forge wiki init` (one-time) | no | `.agent-forge/contexts.yaml` (auto-detected areas) |
| **gather** | `agent-forge wiki gather` (weekly) | no | `.agent-forge/raw/cache/*.json` |
| **compile** | `agent-forge wiki compile` (monthly) | yes | `.agent-forge/curated/*.md` |
| **present** | every chat turn (auto) | no | WIKI system-prompt section (per-area when `contexts.yaml` exists) |
| **ratchet** | `/ratchet`, `--ratchet`, or `wiki ratchet --session ID` | yes | `raw/notes/session/<sid>.md` |
| **compact** | `agent-forge wiki compact` (quarterly) | yes | rewrites `curated/*.md` |
| **maintain** | `agent-forge wiki maintain` (weekly) | no | re-gathers stale areas |
| **metrics** | every chat turn (auto) + `/wrong` | no | `.agent-forge/metrics/*.jsonl` |

The `present` stage uses *section-aware skeleton extraction* on `AGENTS.md` /
`CONTRIBUTING.md` / `README.md`: every `##` and `###` heading is preserved
(plus a few bullets per section), so the agent sees the full structure of
project rules even for very long files. A `_(skeleton — full file: AGENTS.md)_`
marker tells the agent to read the file in full when it needs detail.

### Workflow cheat sheet

```bash
# Day 0 — first touch
agent-forge wiki init           # auto-detect areas → contexts.yaml
agent-forge wiki gather         # pull signal into raw/

# Daily — chat normally; opt into ratchet to remember sessions
agent-forge --ratchet

# Weekly — refresh raw signal, hot-area top-up
agent-forge wiki gather
agent-forge wiki maintain

# Monthly — synthesise raw/ into narrative curated/ pages (LLM)
agent-forge wiki compile

# Quarterly — lint curated/ for staleness / contradictions (LLM)
agent-forge wiki compact

# Anytime — health check
agent-forge wiki status        # CLI: counts of artifacts on disk
# in the REPL:
# /wiki                        # citation rate, override rate, stale areas
# /wrong <correction>          # log an override when the wiki was wrong
```

### Layout under `.agent-forge/`

```
.agent-forge/
├── contexts.yaml         declared areas (optional but recommended)
├── raw/
│   ├── cursor.json       last gather timestamp (incremental marker)
│   ├── cache/            commits, PRs, hotspots, code markers, repo files
│   └── notes/            hand-written + ratchet'd session insights
├── curated/              LLM-synthesised narratives (created by `wiki compile`)
│   ├── onboarding.md
│   ├── hotspots.md
│   ├── adrs.md
│   └── per_area/<area>.md
├── skills/               optional prompt overrides for compile/ratchet/compact
└── metrics/              citations.jsonl · overrides.jsonl · staleness.json
```

Git-ignore the whole directory unless you want to commit curated knowledge
for your team (which is a perfectly good workflow — the markdown is
hand-readable and reviewable).

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
    thinking="medium",  # default; use "off" for cheapest runs
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
| `max_turns` | `int` | `100` | Hard cap on agent turns |
| `thinking` | `str` | `"medium"` | Same levels as CLI |
| `verbose` | `bool` | `False` | Print tool call events |

### Flow states

```
GATING → ISOLATED → EXECUTING → VERIFYING → DELIVERING → DONE
                                                ↓ (any state on error)
                                             FAILED
```

The worktree is always cleaned up (via `try/finally`) on both success and failure.
