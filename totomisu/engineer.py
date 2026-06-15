"""Build pipeline launcher and cross-repo review."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from totomisu.config import (
    BUILD_PHASE_TIMEOUT,
    CAVEMAN_PROMPT,
    REPO_BY_NAME,
    ProjectPaths,
)
from totomisu.parse import parse_cross_review_repos

# Repo whose dev server the ``dev`` commands operate on.
DEV_REPO = "otari-ai"


# ── Tmux helpers ──────────────────────────────────────────────────────

TMUX = "tmux"


def _tmux_session_exists(session: str) -> bool:
    result = subprocess.run(
        [TMUX, "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def _tmux_launch_panes(
    session: str,
    commands: list[tuple[str, str]],
) -> None:
    """Create a tmux session and run commands in parallel panes.

    Each pane uses ``remain-on-exit on`` so that when the command
    finishes, the pane stays visible with ``pane_dead=1``.
    """
    if not commands:
        return

    first_cmd, first_cwd = commands[0]

    subprocess.run(
        [
            TMUX,
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            first_cwd,
            "-x",
            "220",
            "-y",
            "50",
            "/bin/sh",
        ],
        check=True,
    )

    subprocess.run(
        [TMUX, "set-option", "-t", session, "remain-on-exit", "on"],
        check=True,
    )

    subprocess.run(
        [TMUX, "send-keys", "-t", f"{session}:0.0", f"{first_cmd}; exit", "Enter"],
        check=True,
    )

    for cmd, cwd in commands[1:]:
        subprocess.run(
            [TMUX, "split-window", "-t", session, "-c", cwd, "/bin/sh"],
            check=True,
        )
        subprocess.run(
            [TMUX, "send-keys", "-t", session, f"{cmd}; exit", "Enter"],
            check=True,
        )

    if len(commands) > 1:
        subprocess.run(
            [TMUX, "select-layout", "-t", session, "tiled"],
            check=True,
        )


def _tmux_attach(session: str) -> None:
    """Attach to the tmux session so the user can watch."""
    subprocess.run([TMUX, "attach-session", "-t", session])


def _tmux_wait_for_all_panes(
    session: str,
    poll_interval: float = 10.0,
    timeout: float | None = None,
) -> bool:
    """Block until all panes have finished. Returns True if all done.

    Args:
        session: tmux session name.
        poll_interval: Seconds between status checks.
        timeout: Optional max seconds to wait. None means wait forever.
                 If exceeded, logs a warning and returns False.
    """
    start = time.monotonic()
    while True:
        result = subprocess.run(
            [TMUX, "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return True

        statuses = result.stdout.strip().splitlines()
        if statuses and all(s.strip() == "1" for s in statuses):
            return True

        if timeout is not None and (time.monotonic() - start) > timeout:
            elapsed_min = (time.monotonic() - start) / 60
            print(
                f"  [WARN] Timeout after {elapsed_min:.1f} minutes "
                f"waiting for tmux session {session!r}. "
                f"Some panes may still be running.",
                file=sys.stderr,
            )
            return False

        time.sleep(poll_interval)


def _tmux_kill_session(session: str) -> None:
    """Kill a tmux session if it exists."""
    subprocess.run(
        [TMUX, "kill-session", "-t", session],
        capture_output=True,
    )


def _spawn_session_reaper(session: str, poll_interval: int = 5) -> None:
    """Spawn a watchdog pane that kills *session* once all other panes are dead.

    Used for detached (fire-and-forget) launches where no Python caller sticks
    around to call ``_tmux_wait_for_all_panes`` + ``_tmux_kill_session``. The
    reaper itself exits when it calls ``kill-session`` (which destroys it too).

    The panes use ``remain-on-exit on`` so when their work command finishes
    they become ``pane_dead=1`` but the session lingers. This reaper polls
    ``list-panes`` periodically, ignores its own pane (via ``$TMUX_PANE``),
    and kills the session once every non-reaper pane is dead.
    """
    # Inside the reaper pane, tmux sets $TMUX_PANE to its own pane id (e.g.
    # "%12"). We grep it out of the list so we don't wait for ourselves.
    # If list-panes output is empty (e.g. session already gone), bail out.
    sess_q = shlex.quote(session)
    reaper_script = (
        f"while :; do "
        f"out=$({TMUX} list-panes -t {sess_q} -F '#{{pane_id}} #{{pane_dead}}' "
        f"2>/dev/null); "
        f'if [ -z "$out" ]; then exit 0; fi; '
        f'alive=$(printf "%s\\n" "$out" '
        f'| grep -v "^$TMUX_PANE " '
        f"| awk '$2 == 0' | wc -l); "
        f'if [ "$alive" -eq 0 ]; then '
        f"{TMUX} kill-session -t {sess_q}; exit 0; "
        f"fi; "
        f"sleep {poll_interval}; "
        f"done"
    )

    # Split a new (detached) pane running the reaper loop.
    subprocess.run(
        [
            TMUX,
            "split-window",
            "-t",
            session,
            "-d",
            "/bin/sh",
            "-c",
            reaper_script,
        ],
        check=False,
    )

    # Re-tile so the reaper pane doesn't dominate the layout.
    subprocess.run(
        [TMUX, "select-layout", "-t", session, "tiled"],
        capture_output=True,
    )


# ── Build pipeline launcher ──────────────────────────────────────────


def run_build_pipelines(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
) -> None:
    """Launch one tmux pane per repo, each running the full build pipeline.

    Each pane runs ``repo_runner.py`` which sequences:
    engineer -> review -> fix loop -> PR -> CI watch -> done.
    """
    session_name = f"build-{slug}"

    if _tmux_session_exists(session_name):
        print(f"  [info] tmux session {session_name!r} already exists, attaching.")
        _tmux_attach(session_name)
        _tmux_wait_for_all_panes(session_name)
        _tmux_kill_session(session_name)
        return

    print(f"\n── Build (per-repo pipelines) ───────────────────────")
    print(f"  Session: {session_name}")
    print(f"  Repos:   {', '.join(repo_names)}")
    print(f"  Each repo: engineer -> review -> PR -> CI")
    print("────────────────────────────────────────────────────\n")

    paths.logs_dir(slug).mkdir(parents=True, exist_ok=True)

    pane_commands: list[tuple[str, str]] = []
    for name in repo_names:
        cmd = f"totomisu _repo-runner {shlex.quote(slug)} {shlex.quote(name)}"
        pane_commands.append((cmd, str(paths.root)))

    _tmux_launch_panes(session_name, pane_commands)

    print("  Attaching to tmux session. Use Ctrl-B D to detach.\n")
    _tmux_attach(session_name)

    timeout_min = BUILD_PHASE_TIMEOUT / 60
    print(
        f"  Waiting for all repo pipelines to finish "
        f"(timeout: {timeout_min:.0f} min)..."
    )
    all_done = _tmux_wait_for_all_panes(
        session_name, timeout=float(BUILD_PHASE_TIMEOUT)
    )
    _tmux_kill_session(session_name)
    if all_done:
        print("  All repo pipelines done.\n")
    else:
        print(
            "  [WARN] Build phase timed out. Some repos may not have finished.\n"
            "  Completed repos will proceed; others may need manual attention.\n",
            file=sys.stderr,
        )


# ── Cross-repo consistency review ────────────────────────────────────


def run_cross_repo_review(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
) -> None:
    """Single headless agent that checks cross-repo interface alignment."""
    print("\n── Cross-repo Consistency Review ───────────────────")

    tech_spec = paths.spec_file(slug, "tech-spec.md")
    cross_review_file = paths.spec_file(slug, "cross-review.md")

    file_args: list[str] = []
    if tech_spec.exists():
        file_args += ["-f", str(tech_spec)]

    # ── Smart diff selection for cross-review ────────────────────────
    #
    # Instead of truncating the full diff blindly at N chars, we use a
    # two-tier strategy:
    #   1. Full diff for files likely to contain shared contracts (types,
    #      models, schemas, API definitions, interfaces, protos).
    #   2. Stat summary only for everything else.
    #
    # This ensures the cross-review agent sees the interface definitions
    # it needs to verify alignment, without drowning in implementation
    # details.

    _MAX_CONTRACT_DIFF_CHARS = 15000  # per repo, for contract files only
    _MAX_FULL_DIFF_CHARS = 8000  # fallback: if no contract files found

    # Patterns for files likely to contain shared API contracts.
    _CONTRACT_PATTERNS = (
        "**/types.*",
        "**/models.*",
        "**/schema*",
        "**/api.*",
        "**/interface*",
        "**/proto*",
        "**/*.proto",
        "**/openapi*",
        "**/contract*",
        "**/routes.*",
        "**/endpoints.*",
    )

    diff_sections: list[str] = []
    for name in repo_names:
        wt_path = paths.worktree_path(slug, name)
        if not wt_path.exists():
            continue
        repo_info = REPO_BY_NAME.get(name)
        base_branch = repo_info.default_branch if repo_info else "main"
        diff_range = f"origin/{base_branch}...HEAD"

        # Always get the stat summary (lightweight).
        stat_result = subprocess.run(
            ["git", "diff", diff_range, "--stat"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        stat_text = stat_result.stdout.strip() if stat_result.stdout.strip() else ""

        # Try targeted contract-file diffs first.
        contract_diff_parts: list[str] = []
        for pattern in _CONTRACT_PATTERNS:
            result = subprocess.run(
                ["git", "diff", diff_range, "--", pattern],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
            )
            if result.stdout.strip():
                contract_diff_parts.append(result.stdout.strip())

        contract_diff = "\n".join(contract_diff_parts)

        if contract_diff:
            # Truncate contract diffs at a generous limit.
            if len(contract_diff) > _MAX_CONTRACT_DIFF_CHARS:
                contract_diff = (
                    contract_diff[:_MAX_CONTRACT_DIFF_CHARS]
                    + f"\n\n... (truncated, {len(contract_diff)} chars total)"
                )
            diff_text = contract_diff
            diff_label = "Contract/interface files diff"
        else:
            # No contract files found -- fall back to full diff with
            # the original truncation limit.
            diff_result = subprocess.run(
                ["git", "diff", diff_range],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
            )
            diff_text = diff_result.stdout.strip() if diff_result.stdout.strip() else ""
            if len(diff_text) > _MAX_FULL_DIFF_CHARS:
                diff_text = (
                    diff_text[:_MAX_FULL_DIFF_CHARS]
                    + f"\n\n... (truncated, {len(diff_result.stdout)} chars total)"
                )
            diff_label = "Diff"

        section_parts = [f"### {name}"]
        if stat_text:
            section_parts.append(f"**Changed files:**\n```\n{stat_text}\n```")
        if diff_text:
            section_parts.append(f"**{diff_label}:**\n```diff\n{diff_text}\n```")
        if stat_text or diff_text:
            diff_sections.append("\n".join(section_parts))

        # Attach per-repo review and spec files for additional context.
        review = paths.spec_file(slug, f"{name}-review.md")
        if review.exists():
            file_args += ["-f", str(review)]
        spec = paths.spec_file(slug, f"{name}-spec.md")
        if spec.exists():
            file_args += ["-f", str(spec)]

    diffs_text = "\n\n".join(diff_sections) if diff_sections else "No diffs available."

    message = (
        f"You are reviewing changes across multiple repositories for consistency.\n\n"
        f"## Change summaries\n{diffs_text}\n\n"
        f"Check for:\n"
        f"- Interface alignment: do the shared API contracts match across repos?\n"
        f"- Type consistency: are shared types defined the same way?\n"
        f"- Version compatibility: will these changes work together?\n"
        f"- Integration gaps: anything the isolated engineers missed?\n\n"
        f"Write your review to: {cross_review_file}\n\n"
        f"IMPORTANT: At the end of the review file, include a machine-readable "
        f"section with the affected repos:\n"
        f"```json\n"
        f'{{"affected_repos": ["repo-name-1", "repo-name-2"]}}\n'
        f"```\n"
        f"Only list repos that have actionable findings (not informational). "
        f"If there are no actionable findings, use an empty list."
    )

    print(f"  Output: {cross_review_file}")
    print("  Running cross-repo review (headless)...\n")

    result = subprocess.run(
        [
            "opencode",
            "run",
            "--dir",
            str(paths.root),
            "--dangerously-skip-permissions",
            *file_args,
            "--",
            message,
        ],
        cwd=str(paths.root),
    )

    if result.returncode != 0:
        print(
            f"  [WARN] Cross-repo review agent exited with code {result.returncode}",
            file=sys.stderr,
        )

    print("  Cross-repo review complete.\n")


# ── Cross-review fix launcher ────────────────────────────────────────


def _parse_affected_repos_from_cross_review(
    cross_review_file: Path,
    candidate_repos: list[str],
) -> list[str]:
    """Read cross-review.md and return repos that have actionable findings.

    Delegates to ``lib.parse.parse_cross_review_repos`` which tries:
    1. Machine-readable JSON block with ``affected_repos`` key.
    2. Summary-of-findings table rows (excluding "informational").
    3. Returns empty list if neither strategy finds anything.

    Only returns repos that are in *candidate_repos*.
    """
    return parse_cross_review_repos(cross_review_file, candidate_repos)


def run_cross_review_fixes(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
) -> list[str]:
    """Launch one tmux pane per affected repo to fix cross-review findings.

    Returns the list of repos that had findings to fix.
    """
    cross_review_file = paths.spec_file(slug, "cross-review.md")

    if not cross_review_file.exists():
        print("  [skip] No cross-review file found.")
        return []

    # Check if the review even has actionable findings.
    content = cross_review_file.read_text(encoding="utf-8").upper()
    if "PASS" in content and "FINDINGS" not in content:
        print("  [skip] Cross-review passed with no findings.")
        return []

    affected = _parse_affected_repos_from_cross_review(cross_review_file, repo_names)
    if not affected:
        print("  [skip] No actionable findings for the candidate repos.")
        return []

    session_name = f"xfix-{slug}"

    if _tmux_session_exists(session_name):
        print(f"  [info] tmux session {session_name!r} already exists, attaching.")
        _tmux_attach(session_name)
        _tmux_wait_for_all_panes(session_name)
        _tmux_kill_session(session_name)
        return affected

    print("\n── Cross-Review Fix (per-repo pipelines) ───────────")
    print(f"  Session: {session_name}")
    print(f"  Repos:   {', '.join(affected)}")
    print("  Each repo: fix findings -> push -> CI watch")
    print("────────────────────────────────────────────────────\n")

    paths.logs_dir(slug).mkdir(parents=True, exist_ok=True)

    pane_commands: list[tuple[str, str]] = []
    for name in affected:
        cmd = (
            f"totomisu _repo-runner"
            f" {shlex.quote(slug)} {shlex.quote(name)} --fix-cross-review"
        )
        pane_commands.append((cmd, str(paths.root)))

    _tmux_launch_panes(session_name, pane_commands)

    print("  Attaching to tmux session. Use Ctrl-B D to detach.\n")
    _tmux_attach(session_name)

    print("  Waiting for all cross-review fix pipelines to finish...")
    _tmux_wait_for_all_panes(session_name)
    _tmux_kill_session(session_name)
    print("  All cross-review fix pipelines done.\n")

    return affected


# ── Fix PR comments launcher ─────────────────────────────────────────


def run_fix_pr_pipelines(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
    *,
    attach: bool = True,
) -> None:
    """Launch one tmux pane per repo to fix PR review comments.

    Each pane runs ``repo_runner.py --fix-pr`` which fetches PR comments,
    sends the engineer to fix them, and pushes.

    When *attach* is False (e.g. triggered from the dashboard), the tmux
    session is launched but the caller returns immediately.
    """
    session_name = f"fix-pr-{slug}"

    if _tmux_session_exists(session_name):
        if attach:
            print(f"  [info] tmux session {session_name!r} already exists, attaching.")
            _tmux_attach(session_name)
            _tmux_wait_for_all_panes(session_name)
            _tmux_kill_session(session_name)
        return

    print("\n── Fix PR Comments (per-repo pipelines) ────────────")
    print(f"  Session: {session_name}")
    print(f"  Repos:   {', '.join(repo_names)}")
    print("  Each repo: fetch comments -> engineer fix -> push")
    print("────────────────────────────────────────────────────\n")

    paths.logs_dir(slug).mkdir(parents=True, exist_ok=True)

    pane_commands: list[tuple[str, str]] = []
    for name in repo_names:
        wt_path = paths.worktree_path(slug, name)
        if not wt_path.exists():
            print(f"  [{name}] No worktree found, skipping.")
            continue
        cmd = f"totomisu _repo-runner {shlex.quote(slug)} {shlex.quote(name)} --fix-pr"
        pane_commands.append((cmd, str(paths.root)))

    if not pane_commands:
        print("  [skip] No repos with worktrees to fix.")
        return

    _tmux_launch_panes(session_name, pane_commands)

    if attach:
        print("  Attaching to tmux session. Use Ctrl-B D to detach.\n")
        _tmux_attach(session_name)

        print("  Waiting for all fix-PR pipelines to finish...")
        _tmux_wait_for_all_panes(session_name)
        _tmux_kill_session(session_name)
        print("  All fix-PR pipelines done.\n")
    else:
        # Detached launch (e.g. from the dashboard): no Python caller will
        # reap this session. Spawn an in-tmux watchdog pane that kills the
        # session once all work panes have exited.
        _spawn_session_reaper(session_name)


# ── otari-ai dev server (`make dev`) ─────────────────────────────────
#
# These commands operate on the ``otari-ai`` worktree of a session
# (``specs/<slug>/repos/otari-ai``).  ``make dev`` is a long-running dev
# server, so unlike the build pipelines we launch it in a tmux session
# that is NOT auto-reaped on detach: the user keeps the server alive by
# detaching (Ctrl-B D) and tears it down explicitly with ``dev-stop``.


def _dev_session_name(slug: str) -> str:
    return f"dev-{slug}"


def _dev_worktree(slug: str, paths: ProjectPaths) -> Path:
    """Return the otari-ai worktree path, erroring if it does not exist."""
    wt = paths.worktree_path(slug, DEV_REPO)
    if not wt.exists():
        print(
            f"  [ERROR] No {DEV_REPO} worktree for session {slug!r} at {wt}.\n"
            f"  Run the pipeline first (e.g. `totomisu run --resume {slug}`).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return wt


def run_dev_server(slug: str, paths: ProjectPaths) -> None:
    """Launch ``make dev`` for the session's otari-ai worktree in tmux.

    The dev server is long-running; the tmux session is left alive on
    detach so the server keeps running.  Use ``stop_dev_server`` to kill
    it.  If the session already exists, we just re-attach to it.
    """
    for tool in ("make", "tmux"):
        if shutil.which(tool) is None:
            print(f"  [ERROR] {tool} not found on PATH.", file=sys.stderr)
            raise SystemExit(1)

    wt = _dev_worktree(slug, paths)
    session_name = _dev_session_name(slug)

    if _tmux_session_exists(session_name):
        print(
            f"  [info] dev session {session_name!r} already running, attaching.\n"
            f"  Use Ctrl-B D to detach (server keeps running)."
        )
        _tmux_attach(session_name)
        return

    print("\n── Dev server (make dev) ───────────────────────────")
    print(f"  Session: {session_name}")
    print(f"  Repo:    {DEV_REPO}")
    print(f"  Cwd:     {wt}")
    print("────────────────────────────────────────────────────\n")

    # Create a single-pane session running `make dev`.  We do NOT use
    # `_tmux_launch_panes` here: that helper appends `; exit` and sets
    # `remain-on-exit`, which is meant for batch jobs.  The dev server
    # should keep running until explicitly stopped.
    subprocess.run(
        [
            TMUX,
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(wt),
            "-x",
            "220",
            "-y",
            "50",
            "/bin/sh",
        ],
        check=True,
    )
    subprocess.run(
        [TMUX, "send-keys", "-t", f"{session_name}:0.0", "make dev", "Enter"],
        check=True,
    )

    print("  Attaching to tmux session. Use Ctrl-B D to detach (server keeps running).")
    print(f"  Stop with: totomisu dev-stop {slug}\n")
    _tmux_attach(session_name)


def stop_dev_server(slug: str, paths: ProjectPaths) -> bool:
    """Kill the ``make dev`` tmux session for a session.

    Returns ``True`` if a session was killed, ``False`` if none existed.
    """
    session_name = _dev_session_name(slug)
    if not _tmux_session_exists(session_name):
        print(f"  [info] No dev session running for {slug!r}.")
        return False

    _tmux_kill_session(session_name)
    print(f"  Stopped dev session {session_name!r}.")
    return True


def clean_dev(slug: str, paths: ProjectPaths) -> None:
    """Run ``make clean`` in the session's otari-ai worktree.

    Stops any running dev session first (cleaning while the server runs
    can conflict), then runs ``make clean`` in the foreground.
    """
    if shutil.which("make") is None:
        print("  [ERROR] make not found on PATH.", file=sys.stderr)
        raise SystemExit(1)

    wt = _dev_worktree(slug, paths)

    # Stop a running dev server first to avoid conflicts during clean.
    stop_dev_server(slug, paths)

    print("\n── Dev clean (make clean) ──────────────────────────")
    print(f"  Repo: {DEV_REPO}")
    print(f"  Cwd:  {wt}")
    print("────────────────────────────────────────────────────\n")

    result = subprocess.run(["make", "clean"], cwd=str(wt))
    if result.returncode != 0:
        print(
            f"  [WARN] `make clean` exited with code {result.returncode}.",
            file=sys.stderr,
        )
        raise SystemExit(result.returncode)
    print("  Clean complete.\n")
