# agent-forge

A minimal Python coding agent — interactive REPL backed by Claude.

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

Optional — the **agent-forge-wiki skill** indexes your repo's history (commits,
PRs, hotspots, code markers, notes) into schema'd bundles and LLM-compiled
narrative pages. It ships in this repo at `.claude/skills/agent-forge-wiki/`,
auto-discovered by Claude Code agents working here.

```bash
# Run directly (gather → derive → compile):
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py gather --since 2026-02-10
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py compile
```

See the [Wiki skill](#wiki-skill) section below.

---

## Documentation

| If you want to… | Read |
|---|---|
| Install and run your first session | [docs/user/getting-started.md](docs/user/getting-started.md) |
| Configure auth, models, thinking modes, memory | [docs/user/configuration.md](docs/user/configuration.md) |
| Roll out agent-forge to a team | [docs/user/team-setup.md](docs/user/team-setup.md) |
| Look up slash commands or troubleshoot | [docs/user/faq.md](docs/user/faq.md) |
| Connect MCP servers (filesystem, GitHub, Postgres, …) | [docs/user/mcp.md](docs/user/mcp.md) |
| Understand the architecture or modify the codebase | [AGENTS.md](AGENTS.md) |

The agent has six built-in tools — `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Find` — sandboxed to the working directory. Optional **Model Context Protocol** support lets you plug in any number of external tool servers (`pip install agent-forge[mcp]`; see [docs/user/mcp.md](docs/user/mcp.md)).

---

## Wiki skill

A per-repo knowledge system that compounds over time. It gathers signal from
your codebase (commits, PRs, hotspots, code markers, hand-written notes),
optionally synthesises it via an LLM, and surfaces it to agents on demand.

**The wiki is no longer part of agent-forge proper.** It now ships as a
self-contained skill at `.claude/skills/agent-forge-wiki/`, discoverable by
Claude Code agents (and any other skill-aware host) via SKILL.md frontmatter.
This repo includes the skill in-tree; other repos can copy the directory.

State lives under `.agent-forge/` in the target repo (gitignore it).

### Minimum viable usage

```bash
cd ~/your-repo

# First-time area detection (writes .agent-forge/contexts.yaml)
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py init

# Pull repo signal into .agent-forge/raw/
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py gather --since 2026-02-10

# Synthesise narrative cards (LLM call; writes .agent-forge/curated/*.md)
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py compile

# Open Claude Code (or any skill-aware agent); the wiki skill is auto-discovered.
agent-forge
```

Subsequent `gather` runs are incremental (the cursor in
`.agent-forge/raw/cache/.cursor` advances).

Edit `.agent-forge/contexts.yaml` freely — paths use glob syntax (`**`
matches recursively):

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

### Drop hand-written notes anytime

```bash
echo "# Why webhooks retry 3x not 5x" > .agent-forge/raw/notes/webhook-retries.md
```

The next compile picks them up.

### The six stages

Each is a peer subpackage of `.claude/skills/agent-forge-wiki/scripts/wiki/`
with a uniform shape (`__init__.py` + `runner.py`).

| Stage | Trigger | LLM? | Output |
|---|---|---|---|
| **init** | `cli.py init` (one-time) | no | `.agent-forge/contexts.yaml` |
| **gather** | `cli.py gather` (weekly) | no | `.agent-forge/raw/cache/*.json` |
| **compile** | `cli.py compile` (monthly) | yes | `.agent-forge/curated/*.md` |
| **present** | called by build_wiki_section() | no | markdown manifest string |
| **compact** | `cli.py compact` (quarterly) | yes | rewrites `curated/*.md` |
| **maintain** | `cli.py maintain` (weekly) | no | re-gathers stale areas |
| **metrics** | called by record_citation/record_override | no | `.agent-forge/metrics/*.jsonl` |

(*Note: the `ratchet` stage and chat-time `/wiki`, `/wrong`, `--ratchet`
integrations were removed when the wiki was extracted. The skill is
invoked deliberately rather than auto-firing on chat events.*)

### Layout under `.agent-forge/`

```
.agent-forge/
├── contexts.yaml         declared areas (optional but recommended)
├── raw/
│   ├── cursor.json       last gather timestamp (incremental marker)
│   ├── cache/            commits, PRs, hotspots, code markers, repo files
│   └── notes/            hand-written notes
├── curated/              LLM-synthesised narratives (created by `compile`)
│   ├── onboarding.md
│   ├── hotspots.md
│   ├── adrs.md
│   └── per_area/<area>.md
├── skills/               optional prompt overrides for compile/compact
└── metrics/              citations.jsonl · overrides.jsonl · staleness.json
```

Git-ignore the whole directory unless you want to commit curated knowledge
for your team (which is a perfectly good workflow — the markdown is
hand-readable and reviewable).

### Skill architecture reference

For the skill's invocation surface and capabilities, see
[.claude/skills/agent-forge-wiki/SKILL.md](.claude/skills/agent-forge-wiki/SKILL.md).
The stage modules under `scripts/wiki/` are documented inline.


