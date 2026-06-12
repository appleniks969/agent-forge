# forge v2

A coding agent built as a pure decision kernel over one append-only event log.
The kernel does no I/O; every model call, tool run, and verdict is a replayable event.

## Install & run

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
export ANTHROPIC_API_KEY=sk-...       # or CLAUDE_CODE_OAUTH_TOKEN
uv run forge                          # interactive REPL; /help lists commands
uv run forge run -p "list the python files here" --json   # one shot; JSON record on stdout
```

No credentials? Smoke-test offline with the scripted provider:

```sh
uv run forge run -p "hello" --provider fake --json
```

Default model is `claude-sonnet-4-5`; override with `--model <id>` or `FORGE_MODEL`.
In one-shot mode, place flags after `run` — flags before the subcommand are ignored.

## Add a tool

Write a class with a `spec` and an async `run`. Tools return error results, never raise.

```python
from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx

class WordCount:
    spec = ToolSpec(
        name="WordCount",
        description="Count words in a file.",
        params={"type": "object",
                "properties": {"path": {"type": "string", "format": "path"}},
                "required": ["path"]},
        effects=Effects.READ_PATH,
    )

    async def run(self, args, ctx: ToolCtx) -> ToolResult:
        text = ctx.ws.resolve(args["path"]).read_text()
        return ToolResult(call_id="", content=str(len(text.split())))
```

Wire it: add `WordCount()` to the tuple returned by `builtin_tools()` in
`forge/adapters/tools/__init__.py`.

The spec's `effects` and `format: "path"` annotations buy:

- containment — every path arg resolves inside the workspace before `run`; escapes are blocked
- scheduling — read-only calls run in parallel; `WRITE_PATH`/`EXEC`/`EXTERNAL` serialize
- guarding — `EXTERNAL` makes the permission chain Ask before the tool runs

## Connect an MCP server

```sh
uv sync --extra mcp
```

Declare servers in `~/.agent-forge/mcp.toml` (global) or `./.agent-forge/mcp.toml`
(project — overrides global by server name):

```toml
[servers.fs]
command = "mcp-server-filesystem"
args    = ["/home/me/projects"]      # optional
env     = { GITHUB_TOKEN = "..." }   # optional
enabled = true                       # optional, default true
```

Or per invocation — repeatable, overrides file entries by name, works on `forge`
and `forge run` (after `run`):

```sh
uv run forge --mcp-server 'fs=mcp-server-filesystem /tmp'
```

`--no-mcp` skips the TOML files; explicit `--mcp-server` flags still apply.
A malformed spec exits 2; malformed TOML entries are logged and skipped.
Server tools appear as `<server>__<tool>`. In the REPL, `/mcp` shows per-server
status and `/mcp reconnect <name>` restores one; connect failures land there,
never as a startup crash. Trust default: tools a server does not annotate count
as `WRITE_PATH|EXEC|EXTERNAL`, so the guard Asks before running them.

## Develop

```sh
uv run pytest -q                              # full suite
uv run lint-imports                           # layer law: 2 contracts must hold
uv run python scripts/gen_concept_index.py    # regenerate docs/CONCEPTS.md after public-surface changes
```

## Pointers

- [DESIGN.md](DESIGN.md) — architecture spec
- [docs/CONCEPTS.md](docs/CONCEPTS.md) — generated index of every public symbol
- Event logs: `~/.agent-forge/sessions/<session-id>.jsonl` (override root with `FORGE_SESSIONS_ROOT`)
