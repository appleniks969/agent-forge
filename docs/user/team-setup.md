# Team setup

How to roll out agent-forge across a team: secrets distribution, shared
MCP servers, project memory, CI usage, and recommended defaults.

The single-user [getting-started](getting-started.md) and
[configuration](configuration.md) pages assume one developer on a
laptop. This page is the layer on top of that.

---

## TL;DR — one-page rollout

1. **Pick an auth mode for the team** — Anthropic API key, shared via your
   secrets manager. ([Secrets distribution](#secrets-distribution).)
2. **Commit a project `mcp.toml`** at `<repo>/.agent-forge/mcp.toml` if your
   team needs the same external tools. ([Shared MCP](#shared-mcp-servers).)
3. **Commit `<repo>/.agent-forge/memory.md`** with project conventions, or
   leave it gitignored and let each developer maintain their own.
   ([Shared project memory](#shared-project-memory).)
4. **Use `--prompt` from CI** for one-shot tasks (test triage, PR
   summaries). ([CI usage](#ci--non-interactive-usage).)
5. **Standardise on a model + thinking level** so cost is predictable.
   ([Recommended defaults](#recommended-defaults).)
6. **Gitignore session state.** ([What to commit, what to ignore](#what-to-commit-what-to-ignore).)

---

## Secrets distribution

agent-forge reads credentials from two environment variables. Pick **one
mode** for the team and stick to it — mixing causes silent confusion
because OAuth tokens take precedence (see
[Configuration → Precedence](configuration.md#precedence)).

### Mode A — Shared API key (recommended for teams)

One `ANTHROPIC_API_KEY` distributed through your existing secrets
manager. Easy to revoke, easy to audit on the Anthropic console, no
personal accounts involved.

**1Password CLI:**

```bash
# Once, by the team admin:
op item create --category="API Credential" --title="Anthropic agent-forge" \
  credential="sk-ant-..."

# In each developer's ~/.zshrc:
export ANTHROPIC_API_KEY="$(op read 'op://Engineering/Anthropic agent-forge/credential')"
```

**AWS Secrets Manager:**

```bash
export ANTHROPIC_API_KEY="$(aws secretsmanager get-secret-value \
  --secret-id agent-forge/anthropic --query SecretString --output text)"
```

**HashiCorp Vault:**

```bash
export ANTHROPIC_API_KEY="$(vault kv get -field=key secret/agent-forge)"
```

**direnv (per-repo):** drop `.envrc` at the project root (gitignore it):

```bash
# .envrc — loaded automatically by direnv when you cd into the repo
export ANTHROPIC_API_KEY="$(op read 'op://Engineering/Anthropic agent-forge/credential')"
```

### Mode B — Per-developer OAuth tokens

Each developer logs in with their personal Claude Code OAuth token. No
shared secret to distribute, but no central audit trail either.

```bash
# Each developer adds to their own ~/.zshrc:
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."
```

**Never share OAuth tokens.** They're tied to an individual Anthropic
account.

### Mode C — Hybrid (CI uses API key, developers use OAuth)

```bash
# In ~/.zshrc on a developer machine:
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-..."

# In CI (GitHub Actions, etc.):
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

CI machines won't have `CLAUDE_CODE_OAUTH_TOKEN` set, so they fall
through to the API key. Developers see OAuth take precedence on their
laptops.

> **Precedence reminder.** If both variables are set in the same shell,
> agent-forge always picks `CLAUDE_CODE_OAUTH_TOKEN` first.

---

## Shared MCP servers

If your team needs the same external tools (internal API, shared
filesystem area, your Postgres dev DB), commit an `mcp.toml` at the
project root:

```toml
# <repo>/.agent-forge/mcp.toml — checked into git
[servers.fs]
command = "mcp-server-filesystem"
args    = ["./src", "./docs"]

[servers.docs]
command = "mcp-server-fetch"
args    = ["--allow-host", "wiki.company.internal"]
```

Things to know:

- Project config **overrides** global config (`~/.agent-forge/mcp.toml`)
  by server name.
- **Never commit secrets** in `args` or `env`. Reference env vars
  instead — every developer/CI machine sets them locally:

  ```toml
  [servers.gh]
  command = "mcp-server-github"
  env     = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }
  ```

  Then each developer sets `GITHUB_TOKEN` in their shell.
- Developers can add **personal** servers in `~/.agent-forge/mcp.toml`
  without touching the team's project config.
- Servers fail-soft: a missing executable on one machine doesn't break
  the REPL for everyone — it shows up as `failed` in `/mcp` and other
  servers keep working.

Full schema and CLI flags: [mcp.md](mcp.md).

---

## Shared project memory

`<repo>/.agent-forge/memory.md` is plain markdown, loaded into the system
prompt every session. Two patterns work:

### Pattern A — Commit it (shared team knowledge)

```bash
# <repo>/.agent-forge/memory.md
- Use ruff for linting; never add black/isort to this repo.
- HTTP retries use exponential backoff 1/2/4s, max 3 attempts.
- Database migrations live in alembic/, never in src/models/.
- Tests run via `uv run pytest -q`, not bare `pytest`.
```

Then add `.agent-forge/sessions/` and `.agent-forge/raw/` to
`.gitignore`, but **not** `memory.md`.

Every developer's agent now starts with the same context. New hires
benefit instantly.

### Pattern B — Gitignore it (per-developer notes)

`.gitignore` the entire `.agent-forge/` directory. Each developer
accumulates their own context. Use this when individual workflows
diverge enough that shared memory would be noise.

---

## CI / non-interactive usage

For automated tasks (triage failing tests, draft PR descriptions, run a
codemod), use `--prompt`:

```bash
agent-forge --prompt "Summarise why the test suite is failing and propose a fix"
```

The agent runs to completion, prints its final answer, and exits.

**GitHub Actions example:**

```yaml
# .github/workflows/triage.yml
jobs:
  triage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv tool install .
      - name: Triage failing tests
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          agent-forge --thinking off \
                      --model claude-haiku-4-5 \
                      --prompt "Read pytest-output.log and explain the top 3 failures" \
                      > triage.md
      - uses: actions/upload-artifact@v4
        with:
          name: triage
          path: triage.md
```

**Tips for CI:**

- `--thinking off` and `--model claude-haiku-4-5` keep cost predictable.
- `--cwd <path>` works for monorepos where the agent should be sandboxed
  to one subdirectory.
- Exit code is non-zero only on hard errors (missing key, etc.). Parse
  stdout to decide pass/fail for your job.
- Session state under `~/.agent-forge/sessions/` accumulates — wipe it
  in your job's cleanup step or use a fresh runner.

---

## Recommended defaults

For most teams, these defaults strike the cost/quality balance well:

| Setting | Recommended | Why |
|---|---|---|
| `--model` | `claude-sonnet-4-6` | Default. Strong reasoning, 1 M-token context, mid-tier cost. |
| `--thinking` | `medium` | Best quality-per-dollar in our eval matrix. |
| Auth mode | Shared API key | Central audit + revocation. |
| MCP | Project `mcp.toml`, commit-ed | Everyone gets the same tools. |
| Memory | Commit `memory.md` | New hires get team conventions for free. |

Override per-task:

- **Quick edits / lookups:** `--model claude-haiku-4-5 --thinking off`
- **Architecture work / complex debugging:** `--model claude-opus-4-7 --thinking high`

---

## What to commit, what to ignore

Suggested `.gitignore` rules:

```gitignore
# agent-forge per-developer state
.agent-forge/sessions/
.agent-forge/raw/
.agent-forge/curated/        # wiki output (regenerable)
.agent-forge/metrics/

# Keep team-shared
# - .agent-forge/memory.md   (project conventions, hand-edited)
# - .agent-forge/mcp.toml    (MCP server config — no secrets!)
# - .agent-forge/contexts.yaml (wiki area definitions)
```

| File | Commit? | Why |
|---|---|---|
| `<repo>/.agent-forge/memory.md` | yes (Pattern A) or no (Pattern B) | Team-shared context vs. personal notes |
| `<repo>/.agent-forge/mcp.toml` | yes — **no secrets** | Shared MCP servers |
| `<repo>/.agent-forge/contexts.yaml` | yes | Wiki area definitions |
| `<repo>/.agent-forge/sessions/` | no | Per-developer JSONL transcripts |
| `<repo>/.agent-forge/raw/` | no | Wiki gather output, regenerable |
| `<repo>/.agent-forge/curated/` | optional | Wiki narrative cards — commit if you want PR review on them |
| `~/.agent-forge/*` | n/a | Lives outside the repo |

---

## Operational concerns

### Cost monitoring

Each session prints token usage and dollar cost in the footer. For
team-wide tracking, use the Anthropic console — the shared API key
makes per-project usage visible in one place. For per-developer
attribution under a shared key, tag spending via the workspace feature
on the Anthropic console.

### Data handling

- All tool calls are sandboxed to `--cwd`. The agent **cannot** read or
  write outside the working directory.
- The agent does send file contents to the Anthropic API as
  conversation context — review your data-handling policy before
  pointing it at sensitive code.
- `Edit` and `Write` overwrite files without prompting. Commit before
  running risky tasks. `Ctrl-C` interrupts a turn immediately.

### Upgrading the team

```bash
cd ~/code/agent-forge
git pull
bash install.sh
```

`uv tool install .` is idempotent; it replaces the existing install.
Sessions, memory, and MCP config are preserved across upgrades.

---

## See also

- [Configuration](configuration.md) — full CLI reference, all env vars
- [MCP](mcp.md) — full MCP server schema and lifecycle
- [FAQ](faq.md) — slash commands, troubleshooting
- [AGENTS.md](../../AGENTS.md) — architecture (for contributors)
