"""
gather/builtin/git_history.py — pull commits, reverts, file churn from git.

One gatherer covering several signal types from `git log`:

  * Each commit on the default branch becomes a `commit` Artifact.
  * Reverts (subject starting with "Revert ") are *also* emitted as a
    `revert` Artifact, with `reverts_sha` populated when parseable.
  * Conventional-commit prefixes (`fix:`, `feat:`, `refactor:`, …) are
    parsed into signals.
  * Issue/PR cross-refs (`Fixes #123`, `Closes #456`) are parsed into signals.
  * `Co-authored-by:` trailers are parsed into signals.

The cursor key is `git_history.last_sha` (the SHA we wrote on the previous
run); the next run pulls everything `<last_sha>..HEAD`. First run uses the
`since` datetime.

Local + free + universal (assumes `git` is installed; gracefully no-ops if
not, or if cwd is not a git repo).
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent_forge._subprocess import run as run_subprocess

from ...storage import areas_for_paths, load_contexts
from ...types import Artifact, Gatherer, Source

# git log format — fields separated by an unlikely sentinel; commits separated
# by another. Keep the format stable; parsing depends on it.
_FIELD_SEP = "\x1f"   # ASCII unit separator
_REC_SEP = "\x1e"    # ASCII record separator
_LOG_FORMAT = _FIELD_SEP.join([
    "%H",   # full SHA
    "%P",   # parent SHAs (space-separated)
    "%an",  # author name
    "%ae",  # author email
    "%ct",  # committer timestamp (unix)
    "%s",   # subject
    "%b",   # body
    "%(trailers:key=Co-authored-by,valueonly)",  # co-authors
]) + _REC_SEP

_CONV_COMMIT_RE = re.compile(r"^(fix|feat|refactor|perf|chore|docs|test|build|ci|style|revert)(\([^)]+\))?(!)?:\s")
_REVERT_SHA_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_ISSUE_REF_RE = re.compile(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_BUGFIX_PREFIXES = ("fix:", "fix(", "bugfix:", "bugfix(", "hotfix:", "hotfix(")

_MAX_COMMITS_PER_RUN = 5_000


async def _git(args: list[str], cwd: Path, timeout: float = 30.0):
    return await run_subprocess(["git", *args], cwd=str(cwd), timeout=timeout)


async def _is_git_repo(cwd: Path) -> bool:
    if not shutil.which("git"):
        return False
    res = await _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=5.0)
    return res.returncode == 0 and res.stdout.strip() == "true"


async def _default_branch(cwd: Path) -> str:
    """Best-effort default-branch detection. Falls back to HEAD."""
    res = await _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd, timeout=5.0)
    if res.returncode == 0:
        # Format: refs/remotes/origin/main
        ref = res.stdout.strip().rsplit("/", 1)[-1]
        if ref:
            return ref
    return "HEAD"


def _parse_log_output(text: str) -> list[dict]:
    """Parse the formatted log into list of dicts."""
    out: list[dict] = []
    for record in text.split(_REC_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 8:
            continue
        sha, parents, an, ae, ct, subject, body, coauthors = fields[:8]
        try:
            ts = datetime.fromtimestamp(int(ct), tz=timezone.utc)
        except ValueError:
            ts = datetime.now(tz=timezone.utc)
        out.append({
            "sha": sha,
            "parents": parents.split() if parents else [],
            "author_name": an,
            "author_email": ae,
            "ts": ts,
            "subject": subject,
            "body": body,
            "coauthors": [c.strip() for c in coauthors.splitlines() if c.strip()],
        })
    return out


async def _files_for_sha(sha: str, cwd: Path) -> list[str]:
    """Return paths touched by `sha`. Uses `git diff-tree` which works for both
    initial and subsequent commits (and avoids the `--no-patch` + `--name-only`
    incompatibility of `git show`)."""
    res = await _git(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", sha],
        cwd, timeout=10.0,
    )
    if res.returncode != 0:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def _classify(subject: str, body: str) -> dict:
    """Derive signal flags from the commit subject + body."""
    s = subject.strip()
    sl = s.lower()
    sig: dict = {}

    cm = _CONV_COMMIT_RE.match(s)
    if cm:
        sig["conv_type"] = cm.group(1).lower()
        if cm.group(2):
            sig["conv_scope"] = cm.group(2).strip("()")

    if any(sl.startswith(p) for p in _BUGFIX_PREFIXES):
        sig["is_bugfix"] = True

    if s.startswith("Revert "):
        sig["is_revert"] = True
        m = _REVERT_SHA_RE.search(body or "")
        if m:
            sig["reverts_sha"] = m.group(1)

    refs = sorted({int(m.group(1)) for m in _ISSUE_REF_RE.finditer(s + "\n" + (body or ""))})
    if refs:
        sig["fixes_issues"] = refs

    return sig


class GitHistoryGatherer(Gatherer):
    name = "git_history"
    timeout_seconds = 90

    async def gather(self, repo_root: Path, since: datetime, cursor: dict) -> list[Artifact]:
        if not await _is_git_repo(repo_root):
            return []

        # Load contexts.yaml once per run so we can attribute commits to the
        # right area at gather time. Per-area pages used to be empty because
        # this attribution was missing — every commit had area=None and the
        # bundle's per-area filter dropped all of them.
        areas_map, _ = load_contexts(repo_root)

        my_cursor = cursor.get(self.name) or {}
        last_sha: str | None = my_cursor.get("last_sha")

        # Build the rev range. If we have a last_sha, pull <sha>..HEAD;
        # otherwise pull everything since the `since` datetime, capped.
        branch = await _default_branch(repo_root)
        rev_args: list[str]
        if last_sha:
            rev_args = [f"{last_sha}..{branch}"]
        else:
            iso = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
            rev_args = [branch, f"--since={iso}"]

        log_args = ["log", *rev_args, f"--pretty=format:{_LOG_FORMAT}",
                    f"-n{_MAX_COMMITS_PER_RUN}"]
        res = await _git(log_args, repo_root, timeout=self.timeout_seconds)
        if res.returncode != 0:
            return []
        commits = _parse_log_output(res.stdout)
        if not commits:
            return []

        out: list[Artifact] = []
        for c in commits:
            sig = _classify(c["subject"], c["body"])
            if c["coauthors"]:
                sig["coauthors"] = c["coauthors"]
            if c["parents"] and len(c["parents"]) > 1:
                sig["is_merge"] = True

            files = await _files_for_sha(c["sha"], repo_root)
            if files:
                sig["files_changed"] = files

            sig["author"] = c["author_name"]

            # Area attribution: pick the first area whose globs match any file
            # in this commit. Multi-area commits keep the full set under
            # signals.areas so per-area filters can still find them; the top
            # `area` field gets the deterministic primary.
            primary_area: str | None = None
            if files and areas_map:
                matched = areas_for_paths(files, areas_map)
                if matched:
                    primary_area = sorted(matched)[0]
                    if len(matched) > 1:
                        sig["areas"] = sorted(matched)

            short = c["sha"][:12]
            out.append(Artifact(
                id=f"commit-{short}",
                kind="commit",
                source=Source.BUILTIN,
                title=c["subject"][:120],
                body=c["body"] or "",
                ts=c["ts"],
                area=primary_area,
                signals=sig,
            ))

            # Reverts get a second artifact with kind="revert" so hotspots /
            # compile can scan them cheaply without re-classifying.
            if sig.get("is_revert"):
                out.append(Artifact(
                    id=f"revert-{short}",
                    kind="revert",
                    source=Source.BUILTIN,
                    title=c["subject"][:120],
                    body=c["body"] or "",
                    ts=c["ts"],
                    area=primary_area,
                    signals={**sig, "commit_sha": c["sha"]},
                ))

        # Advance cursor to the newest commit we just saw (commits[0] when
        # git log outputs newest-first, which is the default).
        new_last = commits[0]["sha"]
        cursor[self.name] = {**my_cursor, "last_sha": new_last}

        return out
