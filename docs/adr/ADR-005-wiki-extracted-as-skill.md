# ADR-005: Wiki extracted as a Claude Code skill

- **Status:** Accepted
- **Date:** 2026-05-10
- **Deciders:** Nikhil Salunke
- **Supersedes:** ADR-004 (wiki-as-data-layer — wiki as an in-tree subpackage)

## Context

The wiki subsystem was originally an in-tree subpackage at
`agent_forge/wiki/` — ~3K LOC, 17 modules, 207+ tests, with three lazy
integration points into the chat runtime (`/wiki`, `/wrong`, `/ratchet`
REPL commands; `--ratchet` CLI flag; a WIKI section in the system prompt).

It was deliberately structured as composition-only — none of the 17 flat
core modules imported from `wiki/`. The two consumers (`chat.py` and
`prompts.py`) both lazy-imported it, so `import agent_forge` worked with
`wiki/` broken or absent.

This was clean. But:

1. The wiki accounted for ~half the package surface area while serving a
   single use case (repo-knowledge indexing) that wasn't conceptually part
   of "the agent runtime."
2. The agentskills.io specification provides a natural delivery mechanism
   for domain capabilities like this: `SKILL.md` + `scripts/` + auto-discovery
   by Claude Code agents (and any other skill-aware host).
3. Future "wiki-shaped" domain capabilities (on-call, compliance, customer
   briefs) would each want the same shape — extracting the pattern now
   means new domain skills don't need to re-litigate where they live.

The user's framing crystallised it: **agent-forge is execution only.
Domain capabilities are skills.**

## Decision

Extract `agent_forge/wiki/` to a self-contained Claude Code skill at
`.claude/skills/agent-forge-wiki/`:

```
.claude/skills/agent-forge-wiki/
├── SKILL.md                    spec-conformant frontmatter + body
├── scripts/
│   └── wiki/                    the moved Python package
│       ├── __init__.py
│       ├── _llm.py, _noise.py, storage.py, types.py
│       ├── lib/subprocess_run.py    vendored from agent_forge/_subprocess.py
│       ├── compact/, compile/, gather/, maintain/, metrics/, present/
│       └── (ratchet/ deleted — see below)
├── tests/                       the moved tests + conftest.py
├── references/                  ARCHITECTURE.md, SCHEMAS.md, EXTENSION.md
└── assets/skills/               (placeholder for C3 — sharpened compile prompts)
```

Same commit also:

- Drops the **ratchet** stage entirely (session-end summarization). The
  three REPL commands and the `--ratchet` CLI flag are removed.
- Removes `SectionName.WIKI` from the system prompt. The wiki is now
  discovered via the skill's 100-token `description` frontmatter instead
  of a bespoke per-turn injection.
- Replaces `agent-forge wiki <action>` with a one-release deprecation
  shim that points users at the skill scripts directly.

## Consequences

### Positive

- agent-forge core shrinks dramatically (~3K LOC removed, ~206 of the
  package's 413 tests now live in the skill).
- The skill is discoverable by any spec-compliant host (Claude Code today;
  other agents in future).
- New domain capabilities (on-call, compliance) follow the same pattern —
  no precedent battle.
- `.agent-forge/` per-repo state is unchanged; existing repos with wiki
  data don't need to migrate.
- The agent-forge runtime now has a single responsibility: execution.

### Negative / trade-offs

- The wiki is no longer self-bootstrapping from the agent runtime — users
  must invoke `python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py`
  rather than `agent-forge wiki`. Mitigated by the one-release deprecation
  shim.
- The skill *soft-deps* on `agent_forge.{messages, models, provider}` for
  OAuth dispatch, retry, and JSON repair. Full SDK-direct decoupling is a
  follow-up; until then, the skill assumes agent-forge is on the Python
  path (true in this repo; users in other repos must `pip install agent-forge`
  to compile).
- Lose the "WIKI section auto-injected every turn" ergonomics. Agents now
  read `.agent-forge/curated/*.md` via the `Read` tool, guided by the
  skill description — works fine for Claude Code, less obvious in other
  hosts.
- The chat-time `/wrong` correction-logging feature is gone. Anyone who
  was using it can call `wiki.metrics.record_override` directly.
- One pip package + one skill directory now to coordinate; previously
  one tree.

### Mitigations / future work

- **C3 (next commit on this branch):** Split today's `DEFAULT_SKILL`
  triple-quoted string in `compile/runner.py` into four spec-conformant
  per-output `SKILL.md` files under `assets/skills/`, sharpened per
  output (`wiki-compile-{onboarding,hotspots,adrs,per-area}.md`).
- **Follow-up branch:** Replace `agent_forge.provider`-based LLM calls in
  the skill with direct `anthropic` SDK calls, eliminating the soft-dep
  on agent-forge package internals.
- **Follow-up branch:** Port the rich in-tree `wiki/AGENTS.md` content
  into `.claude/skills/agent-forge-wiki/references/ARCHITECTURE.md` (the
  prior file was deleted as part of pre-extraction WIP; the canonical
  pipeline description lives in this skill's SKILL.md body for now).

## Alternatives considered

### A. Keep the wiki in-tree

Status-quo. Costs:
- agent-forge stays oversized for its primary job (agent execution).
- Future wiki-shaped capabilities have no precedent for "where do
  these go?"
- The chat-runtime ↔ wiki coupling (manifest injection, /ratchet hook)
  is a tax that doesn't pay back outside the wiki use case.

Rejected because the size-vs-responsibility mismatch was real and growing.

### B. Extract to a sibling pip package (`agent-forge-wiki`)

Two pip packages, both versioned, with `agent-forge-wiki` depending on a
small `agent-forge-core` utility lib for shared provider/messages/models.

Rejected because:
- Two pip packages adds coordination overhead disproportionate to the
  benefit when there's only one wiki today.
- Doesn't get the spec-driven discovery (`SKILL.md` description) that
  skill packaging provides.
- Doesn't generalise to future domain capabilities (each would also be a
  pip package).

### C. Extract `pipeforge_core` as a shared framework, build wiki + others on top

Considered and rejected in the design discussion preceding this ADR. A
seven-protocol framework (Storage, Gatherer, BundleBuilder, Reconciler,
Partition, …) was YAGNI with N=1 consumer. The honest shared surface is
~1 file (the reconciler) plus a conventions doc — not a framework.

Status: deferred. Re-evaluate after a second skill exists. The shared
surface, if it materialises, lands in a sibling commit; for now each
skill is self-contained.

### D. Skills with their own Python (the chosen path)

The agentskills.io spec explicitly supports `scripts/` directories with
arbitrary Python. With modern Anthropic SDK handling retry/streaming/etc.
natively, the "shared subtle correctness" objection to per-skill code is
much weaker than it appeared. Each skill ships its own scripts; the only
shared dependency is the `anthropic` SDK (and, temporarily, the
agent-forge package for messages/models/provider — see follow-up above).

## Verification

```bash
# 413 tests pass (206 agent-forge core + 207 wiki skill)
uv run pytest tests/ .claude/skills/agent-forge-wiki/tests/ -q

# Skill is auto-discovered by Claude Code in this repo
ls .claude/skills/agent-forge-wiki/SKILL.md

# Deprecation shim works
agent-forge wiki gather
# → "agent-forge wiki has moved to a skill."
# → "Run instead: python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py gather"

# Direct invocation works
python .claude/skills/agent-forge-wiki/scripts/wiki/gather/cli.py status
```

## References

- agentskills.io specification: <https://agentskills.io/specification>
- Prior architecture (now removed): `agent_forge/wiki/AGENTS.md`
  (was the canonical wiki-internals doc; content to be ported to
  `references/ARCHITECTURE.md` in a follow-up)
- Design discussion: conversation history culminating in user direction
  *"agent-forge is execution only. We should think others as skills"*
