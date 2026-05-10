"""
gather/cli.py — `agent-forge wiki gather` argparse subcommand.

Mounted by chat.py:main() via `build_parser`. Standalone-runnable as
`python -m agent_forge.wiki.gather.cli` for development.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..storage import (
    contexts_path, curated_dir, gather_log_path, load_contexts,
    raw_cache_dir, raw_dir, raw_notes_dir, read_cursor,
)
from .discovery import run_gather


def _add_action_subparsers(action_sub: argparse._SubParsersAction) -> None:
    """Mount the wiki actions (gather, status) on the given action-level subparsers.

    Shared between `build_parser` (used by chat.py to mount under `wiki`) and
    `_main` (standalone entry, args already past the `wiki` token).
    """
    g = action_sub.add_parser("gather", help="Pull new signal into .agent-forge/raw/.")
    g.add_argument("--cwd", default=os.getcwd(), help="Repository root (default: cwd)")
    g.add_argument(
        "--since",
        default=None,
        help="ISO date (YYYY-MM-DD) for first-time gather. Default: 1 year ago.",
    )
    g.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run only this gatherer (repeatable). Names: notes, repo_files, "
             "code_markers, git_history, prs, hotspots.",
    )
    g.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-source breakdown; print one summary line.",
    )
    g.set_defaults(_handler=_handle_gather)

    s = action_sub.add_parser("status", help="Show what's currently in .agent-forge/.")
    s.add_argument("--cwd", default=os.getcwd())
    s.set_defaults(_handler=_handle_status)

    i = action_sub.add_parser(
        "init",
        help="Scaffold .agent-forge/contexts.yaml from observed repo layout.",
    )
    i.add_argument("--cwd", default=os.getcwd())
    i.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing contexts.yaml (default: refuse to overwrite).",
    )
    i.set_defaults(_handler=_handle_init)

    c = action_sub.add_parser(
        "compile",
        help="Compile raw/ artifacts into curated/ narratives (LLM call).",
    )
    c.add_argument("--cwd", default=os.getcwd())
    c.add_argument(
        "--only", action="append", default=None,
        help="Compile only this output (repeatable). E.g. onboarding, hotspots, adrs, area:payments.",
    )
    c.add_argument("--dry-run", action="store_true", help="List planned outputs; don't call the LLM.")
    c.add_argument("--model", default=None, help="Model id; default = DEFAULT_MODEL.")
    c.set_defaults(_handler=_handle_compile)

    cp = action_sub.add_parser(
        "compact",
        help="Lint curated/ to merge / dedupe / prune entries (LLM call, monthly).",
    )
    cp.add_argument("--cwd", default=os.getcwd())
    cp.add_argument("--dry-run", action="store_true")
    cp.add_argument("--model", default=None)
    cp.set_defaults(_handler=_handle_compact)

    m = action_sub.add_parser(
        "maintain",
        help="Detect stale areas (commits since last gather) and re-gather them.",
    )
    m.add_argument("--cwd", default=os.getcwd())
    m.add_argument("--dry-run", action="store_true",
                   help="Print stale areas without re-gathering.")
    m.add_argument("--threshold", type=int, default=10,
                   help="Min commits since last gather to mark dirty (default 10).")
    m.set_defaults(_handler=_handle_maintain)


def build_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the `wiki` subcommand under an existing argparse subparsers.

    Used by chat.py:main() to attach `wiki` to the top-level CLI.
    """
    wiki = subparsers.add_parser(
        "wiki",
        help="Repository knowledge: gather signal from PRs/commits/docs/notes.",
    )
    wiki_sub = wiki.add_subparsers(dest="wiki_action", required=True)
    _add_action_subparsers(wiki_sub)
    return wiki


# ── Handlers ──────────────────────────────────────────────────────────────────

def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"error: --since must be ISO date (got {s!r})", file=sys.stderr)
        sys.exit(2)


async def _handle_gather(args: argparse.Namespace) -> int:
    root = Path(args.cwd).resolve()
    since = _parse_since(args.since)

    print(f"gather → {root}")
    if not contexts_path(root).exists():
        print("note: no .agent-forge/contexts.yaml found — areas will be unattributed.")

    result = await run_gather(root, since=since, only=args.only)

    if args.quiet:
        print(
            f"+{result.artifacts_added} artifacts, "
            f"areas: {len(result.areas_touched)}, "
            f"errors: {len(result.errors)}"
        )
    else:
        print(f"\nadded {result.artifacts_added} artifacts:")
        if result.by_kind:
            width = max(len(k) for k in result.by_kind)
            for kind, n in sorted(result.by_kind.items(), key=lambda kv: -kv[1]):
                print(f"  {kind:<{width}}  {n:>5}")
        if result.areas_touched:
            print(f"\nareas touched: {', '.join(result.areas_touched)}")
        if result.errors:
            print(f"\nerrors ({len(result.errors)}; see {gather_log_path(root)}):")
            for e in result.errors[:10]:
                print(f"  ! {e}")
            if len(result.errors) > 10:
                print(f"  ... and {len(result.errors) - 10} more")
        print(f"\ncursor advanced → {result.cursor_advanced_to.isoformat(timespec='seconds')}")
    return 0


async def _handle_status(args: argparse.Namespace) -> int:
    root = Path(args.cwd).resolve()
    raw = raw_dir(root)
    if not raw.exists():
        print("no .agent-forge/raw/ — run `agent-forge wiki gather` first.")
        return 0

    # ── raw/ counts (existing behaviour) ──
    cache = raw_cache_dir(root)
    counts: dict[str, int] = {}
    if cache.exists():
        for sub in sorted(cache.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            counts[sub.name] = sum(1 for _ in sub.rglob("*.json"))

    if not counts:
        print(f"raw/ exists but is empty: {raw}")
        # Still continue to print suggestions below so first-touch users
        # learn about init / gather even before any artifacts exist.
    else:
        print(f".agent-forge/raw/cache/ at {cache}:")
        width = max(len(k) for k in counts)
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<{width}}  {n:>5}")

    # ── contributor signal: notes & session insights ──
    notes = raw_notes_dir(root)
    n_hand_notes = sum(1 for _ in notes.glob("*.md")) if notes.is_dir() else 0
    sess_dir = notes / "session"
    n_session = sum(1 for _ in sess_dir.glob("*.md")) if sess_dir.is_dir() else 0
    print()
    print(f"notes/        {n_hand_notes:>5} hand-written, {n_session} session notes")

    # ── curated narratives (from `wiki compile`) ──
    cur = curated_dir(root)
    n_curated = sum(1 for _ in cur.glob("*.md")) if cur.is_dir() else 0
    n_per_area = sum(1 for _ in (cur / "per_area").glob("*.md")) if (cur / "per_area").is_dir() else 0
    print(f"curated/      {n_curated:>5} narratives, {n_per_area} per-area")

    # ── recommendations / warnings ──
    recos: list[str] = []
    cpath = contexts_path(root)
    if not cpath.exists():
        recos.append(
            "no contexts.yaml — run `agent-forge wiki init` so hot files / "
            "maintenance / metrics can be grouped per area."
        )
    else:
        areas, _ = load_contexts(root)
        if not areas:
            recos.append(
                "contexts.yaml present but no areas declared — see comments in the file."
            )

    cursor = read_cursor(root)
    last_run = cursor.get("last_run_ts") if isinstance(cursor, dict) else None
    if not last_run:
        recos.append("no gather has run yet — run `agent-forge wiki gather`.")
    else:
        try:
            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - last_dt).days
            if age_days > 14:
                recos.append(
                    f"last gather was {age_days} days ago — re-run `wiki gather` "
                    "or `wiki maintain` to refresh."
                )
        except (TypeError, ValueError):
            pass

    if n_hand_notes == 0 and n_session == 0 and any(counts.values() if counts else [0]):
        recos.append(
            "no hand-written notes — add markdown under .agent-forge/notes/ "
            "to surface human-authored context to compile."
        )
    if n_curated == 0 and any(counts.values() if counts else [0]):
        recos.append(
            "no curated narratives yet — once you have meaningful raw signal, "
            "run `agent-forge wiki compile` (LLM call) for ranked onboarding pages."
        )

    if recos:
        print()
        print("Suggestions:")
        for r in recos:
            print(f"  • {r}")
    return 0


# ── init: scaffold contexts.yaml from observed layout ────────────────────────

# Top-level dir names that are never areas (build outputs, deps, infra).
_NEVER_AREAS = {
    ".git", ".github", ".idea", ".vscode", ".agent-forge",
    "node_modules", "dist", "build", "target", "out", "bin",
    ".next", ".nuxt", ".cache", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".gradle", ".venv", "venv",
    "vendor", "third_party", "deps", "tmp", "logs",
}


def _detect_areas(root: Path) -> list[tuple[str, list[str]]]:
    """Return [(area_name, [globs]), …] inferred from the repo layout.

    Detection order (first match wins; a single layer of areas is returned):
      1. monorepo: ``packages/*/``  → one area per child
      2. monorepo: ``apps/*/`` or ``services/*/`` → ditto
      3. ``src/*/`` with ≥2 child dirs → ditto
      4. fall back to top-level non-blacklisted directories
    """
    if not root.is_dir():
        return []

    # 1+2: monorepo conventions
    for parent in ("packages", "apps", "services", "crates"):
        p = root / parent
        if p.is_dir():
            children = [
                c for c in p.iterdir()
                if c.is_dir() and not c.name.startswith(".") and c.name not in _NEVER_AREAS
            ]
            if len(children) >= 2:
                return [(c.name, [f"{parent}/{c.name}/**"]) for c in sorted(children, key=lambda x: x.name)]

    # 3: src/<area>/ layout
    src = root / "src"
    if src.is_dir():
        children = [
            c for c in src.iterdir()
            if c.is_dir() and not c.name.startswith(".") and c.name not in _NEVER_AREAS
        ]
        if len(children) >= 2:
            return [(c.name, [f"src/{c.name}/**"]) for c in sorted(children, key=lambda x: x.name)]

    # 4: top-level fallback
    tops = [
        d for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in _NEVER_AREAS
    ]
    return [(d.name, [f"{d.name}/**"]) for d in sorted(tops, key=lambda x: x.name)]


def _render_contexts_yaml(detected: list[tuple[str, list[str]]]) -> str:
    """Render a contexts.yaml string from detected areas, with onboarding comments."""
    header = (
        "# .agent-forge/contexts.yaml — declare logical areas of this repo.\n"
        "# When set, the wiki groups hot files / commits / metrics per area.\n"
        "# Edit this file freely; commented entries are auto-detected starters.\n"
        "#\n"
        "# Schema:\n"
        "#   areas:\n"
        "#     <area-name>:\n"
        "#       paths: [\"glob/**\", ...]\n"
        "#   inline_comment_authors:\n"
        "#     - github-handle\n"
        "\n"
    )
    if not detected:
        return header + "areas: {}\n\n# inline_comment_authors:\n#   - your-handle\n"
    out = [header, "areas:"]
    for name, globs in detected:
        out.append(f"  {name}:")
        out.append("    paths:")
        for g in globs:
            out.append(f'      - "{g}"')
    out.append("")
    out.append("# inline_comment_authors:")
    out.append("#   - your-handle")
    out.append("")
    return "\n".join(out)


async def _handle_init(args: argparse.Namespace) -> int:
    root = Path(args.cwd).resolve()
    target = contexts_path(root)
    if target.exists() and not args.force:
        print(f"refusing to overwrite existing {target.relative_to(root)} (pass --force to overwrite)")
        return 1

    detected = _detect_areas(root)
    if not detected:
        print(f"no candidate areas detected under {root} — writing empty stub.")
    else:
        print(f"detected {len(detected)} area(s):")
        for name, globs in detected:
            print(f"  {name:<20}  {globs[0]}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_contexts_yaml(detected), encoding="utf-8")
    rel = target.relative_to(root) if target.is_relative_to(root) else target
    print(f"\nwrote {rel}. Edit as needed, then run `agent-forge wiki gather`.")
    return 0


# ── Shared helpers for LLM-using subcommands ─────────────────────────────────

def _make_provider_and_model(args: argparse.Namespace):
    """Resolve the provider + model for compile/compact subcommands.

    Returns (provider, model) on success; prints an error and returns
    (None, None) on missing API key / unknown model.
    """
    api_key = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    if not api_key and not getattr(args, "dry_run", False):
        print(
            "error: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY "
            "(or pass --dry-run to skip the LLM call)",
            file=sys.stderr,
        )
        return None, None

    from ...models import DEFAULT_MODEL, Model
    if args.model:
        try:
            model = Model.from_id(args.model)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return None, None
    else:
        model = DEFAULT_MODEL

    if getattr(args, "dry_run", False):
        # No real provider needed for dry-run; the runners short-circuit before
        # calling .stream(). Pass a sentinel to keep the API uniform.
        class _DryProvider:
            async def stream(self, *a, **kw):
                raise AssertionError("dry-run path called provider.stream — bug")
                yield  # pragma: no cover
        return _DryProvider(), model

    from ...anthropic_provider import AnthropicProvider
    return AnthropicProvider(api_key=api_key, cwd=str(args.cwd)), model


# ── Compile / compact / maintain handlers ────────────────────────────────────

async def _handle_compile(args: argparse.Namespace) -> int:
    from ..compile import compile_wiki

    root = Path(args.cwd).resolve()
    provider, model = _make_provider_and_model(args)
    if provider is None:
        return 1

    print(f"compile → {root}/.agent-forge/curated/")
    if args.dry_run:
        print("[dry-run] would compile:")
        from ..compile.runner import _GLOBAL_OUTPUTS
        from ..storage import load_contexts
        names = [s.name for s in _GLOBAL_OUTPUTS]
        areas, _ = load_contexts(root)
        for a in sorted(areas):
            names.append(f"area:{a}")
        for n in names:
            if not args.only or n in args.only:
                print(f"  - {n}")
        return 0

    res = await compile_wiki(root, provider, model, only=args.only)
    if res.written:
        print(f"\nwrote {len(res.written)} file(s):")
        for p in res.written:
            print(f"  {p.relative_to(root)}")
    if res.errors:
        print(f"\nerrors ({len(res.errors)}):", file=sys.stderr)
        for name, msg in res.errors:
            print(f"  ! {name}: {msg}", file=sys.stderr)
        return 2 if not res.written else 0
    return 0


async def _handle_compact(args: argparse.Namespace) -> int:
    from ..compact import compact_wiki

    root = Path(args.cwd).resolve()
    provider, model = _make_provider_and_model(args)
    if provider is None:
        return 1

    print(f"compact → {root}/.agent-forge/curated/")
    res = await compact_wiki(root, provider, model, dry_run=args.dry_run)
    if res.rewrote:
        print(f"rewrote {len(res.rewrote)} file(s):")
        for p in res.rewrote:
            print(f"  {p.relative_to(root)}")
    else:
        print("no changes.")
    if res.errors:
        for name, msg in res.errors:
            print(f"  ! {name}: {msg}", file=sys.stderr)
    return 0


async def _handle_maintain(args: argparse.Namespace) -> int:
    from ..maintain import detect_stale_areas, run_maintain

    root = Path(args.cwd).resolve()
    if args.dry_run:
        stale = detect_stale_areas(root, threshold=args.threshold)
        if not stale:
            print("No stale areas detected.")
            return 0
        print("Stale areas (commits since last gather ≥ threshold):")
        for area, n in stale:
            print(f"  {area:<30}  +{n} commits")
        return 0

    res = await run_maintain(root, threshold=args.threshold)
    print(
        f"maintain: {res.areas_refreshed} area(s) refreshed, "
        f"{res.artifacts_added} new artifact(s)"
    )
    if res.errors:
        for e in res.errors:
            print(f"  ! {e}", file=sys.stderr)
    return 0


# ── Module entry point ────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    """Standalone entry: receives args *after* the `wiki` token.

    Called both by `chat.py:main()` (which strips `wiki` from sys.argv before
    forwarding) and by `python -m agent_forge.wiki.gather.cli` for development.
    """
    p = argparse.ArgumentParser(prog="agent-forge wiki")
    sub = p.add_subparsers(dest="wiki_action", required=True)
    _add_action_subparsers(sub)
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    handler = getattr(args, "_handler", None)
    if handler is None:
        p.print_help()
        return 1
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(_main())
