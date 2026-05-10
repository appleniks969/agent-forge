---
name: agent-forge-wiki
description: >
  Indexes a repository's git/PR/code history into schema'd JSON bundles and
  LLM-compiled narrative cards (onboarding, hotspots, ADRs, per-area pages).
  Use when the user asks about repo onboarding, hot files, recent decisions,
  code ownership, architectural choices, or how to navigate an unfamiliar
  codebase. Pulls from git log, gh PRs, repo files, and code markers; writes
  schema-versioned bundles to .agent-forge/raw/derived/ and narrative pages
  to .agent-forge/curated/. Safe to re-run; gather is idempotent (SHA-deduped),
  compile is the only LLM stage.
compatibility: >
  Requires git, gh (optional, for PR ingestion), Python 3.12+, and
  ANTHROPIC_API_KEY for the compile stage. Reads/writes .agent-forge/ in the
  repo root.
license: MIT
metadata:
  author: agent-forge
  version: "1.0"
  extracted-from: agent_forge/wiki/
allowed-tools: Bash(git:*) Bash(gh:*) Read Write
---

# agent-forge wiki

A deterministic data layer over a repo's history, with an LLM-compiled
narrative on top. Five stages: **pull → derive → compile → present → maintain**.

## When to invoke this skill

Use when the user asks any of:

- *"What should I read first in this codebase?"*
- *"Which files change together / who owns this code?"*
- *"What decisions are recorded here?"*
- *"What's been changing recently?"*
- *"Give me an onboarding tour of this repo."*

## How to invoke

```bash
# First-time: pull external state into .agent-forge/raw/cache/
python .claude/skills/agent-forge-wiki/scripts/gather/cli.py gather --since 2026-02-10

# Re-derive schema'd bundles (cheap, deterministic)
python .claude/skills/agent-forge-wiki/scripts/gather/cli.py derive

# LLM-compile narrative cards into .agent-forge/curated/
python .claude/skills/agent-forge-wiki/scripts/gather/cli.py compile

# Show what's been gathered
python .claude/skills/agent-forge-wiki/scripts/gather/cli.py status
```

After compile, the agent should read `.agent-forge/curated/*.md` directly for
answers — those are the narrative outputs. For programmatic consumers (review
bots, refactor tools), read the schema'd JSON in `.agent-forge/raw/derived/`.

## Outputs

- `.agent-forge/curated/identity.md` — short, deterministic project identity card
- `.agent-forge/curated/onboarding.md` — first-read tour
- `.agent-forge/curated/hotspots.md` — files that change often + who owns them
- `.agent-forge/curated/adrs.md` — decisions recorded in `docs/adr/` and similar
- `.agent-forge/curated/per_area/<area>.md` — per-area narrative

## Architecture reference

See [`references/ARCHITECTURE.md`](references/ARCHITECTURE.md) for the full
pipeline shape, schema contracts, and policies.

See [`references/SCHEMAS.md`](references/SCHEMAS.md) for the schema'd-bundle
envelope and per-bundle data shapes.

## State

Per-repo state lives in `.agent-forge/`:

- `contexts.yaml` — area definitions (path globs) and allowlists
- `raw/cache/<kind>/` — SHA-deduped pulls (regenerable)
- `raw/derived/*.json` — schema'd aggregate bundles (the public API)
- `curated/*.md` — LLM-compiled narrative + `.fingerprints.json` audit
- `skills/<name>/SKILL.md` — optional per-repo overrides of build-time compile prompts
- `gatherers/*.py` — optional user-authored gatherers
