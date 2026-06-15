"""Phase 5: Clone missing repos and create worktrees via wt."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from contextlib import contextmanager
from collections.abc import Generator
from pathlib import Path

from totomisu.config import REPO_BY_NAME, REPOS, ProjectPaths


# ── File locking (for parallel orchestrator safety) ───────────────────


def _locks_dir(paths: ProjectPaths) -> Path:
    d = paths.repos_dir / ".locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _repo_lock(paths: ProjectPaths, repo_name: str) -> Generator[None, None, None]:
    """Acquire an exclusive file lock for a repo.

    Prevents two parallel orchestrator processes from cloning or creating
    worktrees in the same repo simultaneously.
    """
    lock_path = _locks_dir(paths) / f"{repo_name}.lock"
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ── Clone ─────────────────────────────────────────────────────────────


def ensure_repos_cloned(paths: ProjectPaths) -> None:
    """Clone any repository that is not yet present under repos/."""
    paths.repos_dir.mkdir(parents=True, exist_ok=True)

    for repo in REPOS:
        dest = paths.repo_path(repo.name)
        if dest.exists() and (dest / ".git").exists():
            print(f"  [fetch] {repo.name} — updating from origin")
            result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(
                    f"  [warn] fetch failed for {repo.name}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
            continue

        with _repo_lock(paths, repo.name):
            # Re-check after acquiring the lock (another process may have
            # cloned while we were waiting).
            if dest.exists() and (dest / ".git").exists():
                print(f"  [ok]   {repo.name} already cloned (raced)")
                continue

            print(f"  [clone] {repo.name} -> {dest}")
            subprocess.run(
                ["git", "clone", repo.github_url, str(dest)],
                check=True,
            )

            # If the repo's default_branch differs from the remote HEAD
            # (e.g. "develop" vs "main"), check it out now.
            if repo.default_branch != "main":
                subprocess.run(
                    ["git", "checkout", repo.default_branch],
                    cwd=str(dest),
                    capture_output=True,
                )


# ── Pull (update clones to latest) ────────────────────────────────────


def pull_repos(paths: ProjectPaths) -> dict[str, str]:
    """Fast-forward every cloned repo's default branch to origin.

    For each repository under ``repos/`` this:
    1. Fetches the default branch from origin.
    2. Fast-forwards the local default branch to ``origin/<default_branch>``.

    The fast-forward is deliberately non-destructive: repos with
    uncommitted changes, a checked-out non-default branch, or local
    commits that diverge from origin are skipped rather than reset.  This
    keeps ``pull`` safe to run at any time without clobbering local work.

    Returns:
        Mapping of repo name -> outcome string (one of ``"updated"``,
        ``"up-to-date"``, ``"skipped (...)"`` or ``"error (...)"``).
    """
    paths.repos_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}

    for repo in REPOS:
        dest = paths.repo_path(repo.name)
        if not dest.exists() or not (dest / ".git").exists():
            results[repo.name] = "skipped (not cloned)"
            print(f"  [skip] {repo.name} — not cloned")
            continue

        base = repo.default_branch

        with _repo_lock(paths, repo.name):
            # Refuse to touch a dirty working tree.
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            if status.returncode == 0 and status.stdout.strip():
                results[repo.name] = "skipped (uncommitted changes)"
                print(f"  [skip] {repo.name} — uncommitted changes")
                continue

            # Only fast-forward when the default branch is checked out.
            current = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            current_branch = current.stdout.strip() if current.returncode == 0 else ""
            if current_branch != base:
                results[repo.name] = f"skipped (on {current_branch or '???'})"
                print(
                    f"  [skip] {repo.name} — on {current_branch or '???'}, "
                    f"expected {base}"
                )
                continue

            # Fetch the default branch.
            fetch = subprocess.run(
                ["git", "fetch", "origin", base],
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                results[repo.name] = "error (fetch failed)"
                print(
                    f"  [error] {repo.name} — fetch failed: {fetch.stderr.strip()}",
                    file=sys.stderr,
                )
                continue

            # Fast-forward only.  `--ff-only` fails (non-zero) when the
            # local branch has diverged, which we surface as a skip.
            merge = subprocess.run(
                ["git", "merge", "--ff-only", f"origin/{base}"],
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            if merge.returncode != 0:
                results[repo.name] = "skipped (diverged, needs manual merge)"
                print(
                    f"  [skip] {repo.name} — local {base} diverged from origin, "
                    f"manual merge required"
                )
                continue

            if "Already up to date" in merge.stdout:
                results[repo.name] = "up-to-date"
                print(f"  [ok]   {repo.name} — already up to date")
            else:
                results[repo.name] = "updated"
                print(f"  [pull] {repo.name} — fast-forwarded {base} to origin")

    return results


# ── Worktrees ─────────────────────────────────────────────────────────


def create_worktrees(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
) -> dict[str, Path]:
    """Create a worktree for each affected repo.

    Uses ``wt switch --create`` with a custom ``WORKTRUNK_WORKTREE_PATH``
    so the worktrees land inside ``specs/<slug>/repos/<repo>/``.

    Returns:
        Mapping of repo name -> worktree path.
    """
    worktree_root = paths.worktree_dir(slug)
    worktree_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    for name in repo_names:
        if name not in REPO_BY_NAME:
            print(f"  [skip] unknown repo {name!r}", file=sys.stderr)
            continue

        wt_path = paths.worktree_path(slug, name)
        repo_dir = paths.repo_path(name)

        if not repo_dir.exists():
            print(f"  [skip] repo {name!r} not cloned at {repo_dir}", file=sys.stderr)
            continue

        if wt_path.exists():
            print(f"  [ok]   worktree already exists: {wt_path}")
            result[name] = wt_path
            continue

        repo_info = REPO_BY_NAME[name]
        base_branch = repo_info.default_branch

        print(
            f"  [wt]   creating worktree for {name} on branch {slug} (from {base_branch})"
        )

        with _repo_lock(paths, name):
            # Re-check after lock in case another process created it.
            if wt_path.exists():
                print(f"  [ok]   worktree appeared (raced): {wt_path}")
                result[name] = wt_path
                continue

            # Ensure the base branch is fully up to date.
            subprocess.run(
                ["git", "fetch", "origin", base_branch],
                cwd=str(repo_dir),
                capture_output=True,
            )
            # Fast-forward the local base branch to match origin so that
            # tools like `wt` that resolve branch names locally will see
            # the very latest commits.
            subprocess.run(
                [
                    "git",
                    "branch",
                    "-f",
                    base_branch,
                    f"origin/{base_branch}",
                ],
                cwd=str(repo_dir),
                capture_output=True,
            )

            # Override the worktree path template so wt places it where we want.
            env = os.environ.copy()
            env["WORKTRUNK_WORKTREE_PATH"] = str(wt_path)

            try:
                subprocess.run(
                    [
                        "wt",
                        "switch",
                        "--create",
                        slug,
                        "--base",
                        base_branch,
                        "-y",  # skip approval prompts
                        "--no-verify",  # skip hooks
                        "--no-cd",  # don't try to cd
                    ],
                    cwd=str(repo_dir),
                    env=env,
                    check=True,
                )
            except subprocess.CalledProcessError:
                # Fallback: use git worktree directly if wt has issues with
                # the env override.
                print(f"  [fallback] using git worktree add for {name}")
                _git_worktree_add(repo_dir, wt_path, slug, base_branch)

            if wt_path.exists():
                result[name] = wt_path
            else:
                # wt might have placed it elsewhere; try to find it.
                found = _find_wt_path(repo_dir, slug)
                if found:
                    print(f"  [info] wt placed worktree at {found}, symlinking")
                    wt_path.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(found, wt_path)
                    result[name] = wt_path
                else:
                    print(
                        f"  [error] could not locate worktree for {name}",
                        file=sys.stderr,
                    )

    return result


def update_worktrees(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
) -> None:
    """Bring existing worktrees up to date with the latest upstream base branch.

    For each worktree:
    - Fetches the latest base branch from origin.
    - Fast-forwards the local base branch pointer.
    - If the worktree has no local commits beyond the base, hard-resets to
      the latest base.
    - If local commits exist, rebases the feature branch onto the updated
      base.  On conflict the rebase is aborted and a warning is printed
      (the engineer agent has its own rebase logic that can handle it
      later).
    """
    for name in repo_names:
        if name not in REPO_BY_NAME:
            continue

        wt_path = paths.worktree_path(slug, name)
        repo_dir = paths.repo_path(name)

        if not wt_path.exists() or not repo_dir.exists():
            continue

        repo_info = REPO_BY_NAME[name]
        base_branch = repo_info.default_branch

        with _repo_lock(paths, name):
            # 1. Fetch latest base branch.
            fetch = subprocess.run(
                ["git", "fetch", "origin", base_branch],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                print(
                    f"  [warn] fetch failed for {name}: {fetch.stderr.strip()}",
                    file=sys.stderr,
                )
                continue

            # 2. Fast-forward local base branch pointer.
            subprocess.run(
                ["git", "branch", "-f", base_branch, f"origin/{base_branch}"],
                cwd=str(repo_dir),
                capture_output=True,
            )

            # 3. Check for local commits beyond the base in the worktree.
            log_result = subprocess.run(
                ["git", "log", f"origin/{base_branch}..HEAD", "--oneline"],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
            )
            has_local_commits = bool(log_result.stdout.strip())

            if not has_local_commits:
                # No local work yet — just reset to latest base.
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{base_branch}"],
                    cwd=str(wt_path),
                    capture_output=True,
                )
                print(f"  [update] {name} — reset to latest {base_branch}")
            else:
                # Local commits exist — rebase onto updated base.
                rebase = subprocess.run(
                    ["git", "rebase", f"origin/{base_branch}"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                )
                if rebase.returncode != 0:
                    # Abort the failed rebase and warn.
                    subprocess.run(
                        ["git", "rebase", "--abort"],
                        cwd=str(wt_path),
                        capture_output=True,
                    )
                    print(
                        f"  [warn] rebase failed for {name} — conflicts detected, "
                        f"skipping (engineer agent can retry later)",
                        file=sys.stderr,
                    )
                else:
                    print(f"  [update] {name} — rebased onto latest {base_branch}")


def _git_worktree_add(
    repo_dir: Path,
    wt_path: Path,
    branch: str,
    start_point: str = "main",
) -> None:
    """Direct ``git worktree add`` as fallback."""
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(wt_path),
            f"origin/{start_point}",
        ],
        cwd=str(repo_dir),
        check=True,
    )


def _find_wt_path(repo_dir: Path, branch: str) -> Path | None:
    """Ask git where it put the worktree for a given branch."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line.split(" ", 1)[1])
            if branch in candidate.name:
                return candidate
    return None


# ── Context setup ─────────────────────────────────────────────────────


# Sentinel marker used to make spec enrichment idempotent across re-runs.
# If the spec file already starts with this marker, it has been enriched.
_ENRICHED_MARKER = "<!-- totomisu:enriched -->"


def setup_engineer_context(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
) -> None:
    """Enrich the per-repo spec file with scope notes, test hints, and a scoping warning.

    The orchestrator previously wrote an ``AGENTS.md`` into each worktree so
    opencode would auto-load it as project context.  That approach collides
    with opencode's reserved filenames (``AGENTS.md`` and ``CLAUDE.md``) and
    risks polluting PRs with orchestrator-generated files.

    Instead, this function modifies the already-existing spec file at
    ``specs/<slug>/<repo>-spec.md`` in place so that every agent call that
    attaches the spec via ``-f`` (all of them, per ``repo_runner.py``)
    automatically sees the scoping warning and repo-specific notes.

    The function is idempotent: it writes a sentinel comment at the top of
    the file and skips enrichment if the sentinel is already present.
    Nothing is written into the worktree.
    """
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")

    if not spec_file.exists():
        print(f"  [warn] no per-repo spec for {repo_name}, skipping enrichment")
        return

    spec_content = spec_file.read_text(encoding="utf-8")

    # Idempotency guard: skip if already enriched.
    if spec_content.lstrip().startswith(_ENRICHED_MARKER):
        print(f"  [skip] spec for {repo_name} already enriched")
        return

    # Include scope notes and test hints if the repo has any.
    repo_info = REPO_BY_NAME.get(repo_name)
    extra_sections = ""
    if repo_info and repo_info.scope_notes:
        extra_sections += f"## Scope Notes\n\n{repo_info.scope_notes}\n\n---\n\n"
    if repo_info and repo_info.test_hints:
        extra_sections += (
            f"## Testing (IMPORTANT)\n\n"
            f"**NEVER run the full test suite.** The full suite is slow and "
            f"runs automatically in CI after you push. Only run targeted tests "
            f"for the specific files you changed.\n\n"
            f"{repo_info.test_hints}\n\n---\n\n"
        )
    if repo_info and repo_info.pragma_validators:
        validators = ", ".join(f"`/{v}`" for v in repo_info.pragma_validators)
        extra_sections += (
            f"## Enforcement (agent-pragma)\n\n"
            f"This repository is guarded by the following pragma validators: "
            f"{validators}.\n\n"
            f"After your changes, the pipeline runs `/validate`. Any **HARD** "
            f"violation blocks the pipeline and forces a fix round. "
            f"Severity levels:\n\n"
            f"- **HARD** - must fix before proceeding.\n"
            f"- **SHOULD** - fix or justify.\n"
            f"- **WARN** - advisory.\n\n"
            f"Write code that passes `/validate` on the first try: idiomatic "
            f"error handling, no hardcoded secrets, doc-comments on exported "
            f"APIs, no swallowed exceptions.\n\n---\n\n"
        )

    header = (
        f"{_ENRICHED_MARKER}\n"
        f"# Implementation Spec: {slug}\n\n"
        f"THIS FILE WAS GENERATED by the any-llm-world orchestrator.\n"
        f"It contains the implementation spec for this repository.\n\n"
        f"**IMPORTANT: You are working ONLY on the `{repo_name}` repository. "
        f"Do NOT access, read, or modify files outside this repository. "
        f"Do NOT navigate to parent directories or sibling repositories. "
        f"All the context you need is in this file and the code in this directory.**\n\n"
        f"---\n\n"
        f"{extra_sections}"
    )

    enriched = header + spec_content
    spec_file.write_text(enriched, encoding="utf-8")
    print(f"  [ok] enriched spec for {repo_name}")


# ── Resume helpers ────────────────────────────────────────────────────


def worktrees_exist(slug: str, repo_names: list[str], paths: ProjectPaths) -> bool:
    """Check if all expected worktrees exist."""
    return all(paths.worktree_path(slug, name).exists() for name in repo_names)
