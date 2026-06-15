"""Main CLI entry point for totomisu -- the otari multi-repo orchestrator.

Usage:
    totomisu init                              # set up a new workspace
    totomisu run --issue <url>                 # start from a GitHub issue
    totomisu run --prompt "description"        # start from a text prompt
    totomisu run --resume <slug>               # resume a previous run
    totomisu run --resume <slug> --skip-to build
    totomisu dev <slug>                        # run `make dev` (otari-ai) for a session
    totomisu dev-stop <slug>                    # stop the `make dev` tmux session
    totomisu dev-clean <slug>                   # run `make clean` (stops dev first)
    totomisu pull                              # update all clones to latest
    totomisu reset [-y]                        # wipe repos+specs and re-init
    totomisu dashboard [--port 8080]           # launch the web dashboard
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from totomisu.config import (
    PRAGMA_REPO_URL,
    PRAGMA_VERSION,
    REPOS,
    WORKSPACE_MARKER,
    ProjectPaths,
    get_package_data_path,
    get_project_paths,
)
from totomisu.intake import (
    TriageResult,
    classify,
    confirm_triage,
    fetch_issue,
    format_issue_as_input,
    load_triage,
    save_input,
    save_triage,
)
from totomisu.prd import (
    debate_done,
    design_exists,
    prd_exists,
    run_debate,
    run_designer,
    run_designer_headless,
    run_pm,
)
from totomisu.architect import (
    get_affected_repos,
    run_architect,
    run_architect_headless,
    tech_spec_exists,
)
from totomisu.workspace import (
    create_worktrees,
    ensure_repos_cloned,
    pull_repos,
    setup_engineer_context,
    update_worktrees,
    worktrees_exist,
)
from totomisu.costs import check_cost_ceiling, save_costs
from totomisu.engineer import (
    run_build_pipelines,
    run_cross_repo_review,
    run_cross_review_fixes,
)
from totomisu.status import init_status, update_phase


# ── Phase ordering for --skip-to ──────────────────────────────────────

SKIP_TO_PHASES = (
    "intake",
    "workspace",
    "pm",
    "debate",
    "designer",
    "architect",
    "build",
    "cross-review",
    "cross-review-fix",
)


# ── CLI ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="totomisu",
        description="Multi-repo orchestrator for the otari ecosystem.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── init ──────────────────────────────────────────────────────
    init_parser = subparsers.add_parser(
        "init",
        help="Initialise a new totomisu workspace.",
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help=(
            "Directory to use as the workspace.  If omitted, you will be "
            "prompted interactively."
        ),
    )

    # ── run ───────────────────────────────────────────────────────
    run_parser = subparsers.add_parser(
        "run",
        help="Run the orchestration pipeline.",
    )
    run_group = run_parser.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--issue", metavar="URL", help="GitHub issue URL to work on")
    run_group.add_argument(
        "--prompt", metavar="TEXT", help="Free-form prompt describing the work"
    )
    run_group.add_argument(
        "--resume", metavar="SLUG", help="Resume a previous run from its slug"
    )
    run_parser.add_argument(
        "--skip-to",
        metavar="PHASE",
        choices=SKIP_TO_PHASES,
        help=f"Skip to a specific phase (requires --resume). Choices: {', '.join(SKIP_TO_PHASES)}",
    )
    run_parser.add_argument(
        "--ci-check",
        nargs="?",
        const="all",
        metavar="REPO",
        help="Check CI status for all repos (or a specific repo). Requires --resume.",
    )
    run_parser.add_argument(
        "--fix-pr",
        nargs="?",
        const="all",
        metavar="REPO",
        help="Fetch PR review comments and send engineer to fix for all repos (or a specific repo). Requires --resume.",
    )
    run_parser.add_argument(
        "--fix-cross-review",
        nargs="?",
        const="all",
        metavar="REPO",
        help=(
            "Fix cross-review findings for all affected repos (or a specific "
            "repo). Requires --resume."
        ),
    )
    run_parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help=(
            "Run ALL agent phases headlessly (no TUI interaction). "
            "PM, debate, designer, and architect each run as a single-pass "
            "opencode call. No prompts, no TUI; suitable for unattended runs."
        ),
    )

    # ── update ────────────────────────────────────────────────────
    update_parser = subparsers.add_parser(
        "update",
        help=(
            "Update workspace-scoped assets (agent-pragma, bundled agent "
            "files, opencode.json) without re-running `totomisu init`."
        ),
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would change without modifying any files.",
    )

    # ── pull ──────────────────────────────────────────────────────
    subparsers.add_parser(
        "pull",
        help=(
            "Fast-forward every cloned repo's default branch to the latest "
            "upstream. Skips repos with uncommitted changes or divergence."
        ),
    )

    # ── reset ─────────────────────────────────────────────────────
    reset_parser = subparsers.add_parser(
        "reset",
        help=(
            "Destroy and rebuild the workspace: prune all worktrees, delete "
            "repos/ and specs/, then re-run init to re-clone everything."
        ),
    )
    reset_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt (for unattended/scripted use).",
    )

    # ── dashboard ─────────────────────────────────────────────────
    dash_parser = subparsers.add_parser(
        "dashboard",
        help="Launch the web dashboard.",
    )
    dash_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080).",
    )

    # ── dev ───────────────────────────────────────────────────────
    dev_parser = subparsers.add_parser(
        "dev",
        help="Run `make dev` (otari-ai dev server) for a session's worktree.",
    )
    dev_parser.add_argument("slug", help="Session slug to run the dev server for.")

    # ── dev-stop ──────────────────────────────────────────────────
    dev_stop_parser = subparsers.add_parser(
        "dev-stop",
        help="Stop the running `make dev` tmux session for a session.",
    )
    dev_stop_parser.add_argument("slug", help="Session slug whose dev server to stop.")

    # ── dev-clean ─────────────────────────────────────────────────
    dev_clean_parser = subparsers.add_parser(
        "dev-clean",
        help="Run `make clean` in a session's otari-ai worktree (stops dev first).",
    )
    dev_clean_parser.add_argument("slug", help="Session slug to clean.")

    # ── _repo-runner (hidden, used by tmux panes) ─────────────────
    rr_parser = subparsers.add_parser("_repo-runner")
    rr_parser.add_argument("slug")
    rr_parser.add_argument("repo")
    rr_parser.add_argument("--fix-cross-review", action="store_true", dest="rr_fix_xr")
    rr_parser.add_argument("--fix-pr", action="store_true", dest="rr_fix_pr")
    rr_parser.add_argument("--ci-check", action="store_true", dest="rr_ci_check")
    rr_parser.add_argument("--rebase", action="store_true", dest="rr_rebase")

    return parser


def _should_skip(phase: str, skip_to: str | None) -> bool:
    if skip_to is None:
        return False
    try:
        return SKIP_TO_PHASES.index(phase) < SKIP_TO_PHASES.index(skip_to)
    except ValueError:
        return False


def _confirm_continue(phase_name: str, slug: str) -> None:
    """Prompt the user to confirm continuation after an interactive phase.

    Catches accidental early exits from TUI sessions.  Typing 'n' pauses
    the pipeline and prints a resume command.
    """
    answer = input(f"\n  {phase_name} phase complete. Continue? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        print(f"  Pipeline paused after {phase_name}.")
        next_phase_idx = SKIP_TO_PHASES.index(phase_name.lower().replace(" ", "-")) + 1
        if next_phase_idx < len(SKIP_TO_PHASES):
            next_phase = SKIP_TO_PHASES[next_phase_idx]
            print(f"  Resume with: totomisu run --resume {slug} --skip-to {next_phase}")
        sys.exit(0)


# ── Init command ──────────────────────────────────────────────────────


def _check_system_deps() -> list[str]:
    """Check that required system tools are available."""
    missing = []
    for tool in ("opencode", "gh", "git", "tmux"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


_STOCK_OPENCODE_JSON: dict = {
    "$schema": "https://opencode.ai/config.json",
    # Playwright MCP lets the designer agent navigate a running web UI
    # (otari-ai frontend) to keep visual/interaction designs consistent
    # with existing screens.  Enabled globally; only the designer agent
    # is instructed to use it, so other agents simply ignore it.  Needs
    # `npx`/Node on PATH; first run downloads the package + browsers.
    "mcp": {
        "playwright": {
            "type": "local",
            "command": ["npx", "-y", "@playwright/mcp@latest", "--headless"],
            "enabled": True,
        }
    },
}
"""Default contents of the workspace ``opencode.json`` file.

Shared between ``cmd_init`` (writes it on first setup) and
``_refresh_opencode_json`` (refuses to touch a user-modified copy).
Bump this when we intentionally change the stock config.
"""


def _stock_opencode_json_text() -> str:
    """Return the canonical serialised form written to ``opencode.json``."""
    return json.dumps(_STOCK_OPENCODE_JSON, indent=2) + "\n"


def _install_agent_pragma(ws_dir: Path, version: str) -> bool:
    """Clone agent-pragma into the workspace and install into ``.opencode/``.

    Per-workspace install keeps pragma's skills/commands scoped to this
    totomisu workspace without touching the user's global opencode
    config.  Idempotent: reuses an existing clone at the pinned tag and
    re-runs ``make install`` every call so broken symlinks are repaired.

    Returns ``True`` on success (including the "already at <version>"
    path) and ``False`` on any failure.  The ``init`` caller discards
    the result to preserve its warn-and-continue behaviour; ``update``
    uses it to set the process exit code.
    """
    pragma_dir = ws_dir / ".agent-pragma"

    # Check for git/make before starting.
    if shutil.which("git") is None:
        print("  [WARN] git not found on PATH; skipping agent-pragma install.")
        return False
    if shutil.which("make") is None:
        print("  [WARN] make not found on PATH; skipping agent-pragma install.")
        return False

    # Clone (or update) the pragma checkout at the pinned tag.
    if not pragma_dir.exists():
        print(f"  Cloning agent-pragma {version}...")
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                version,
                PRAGMA_REPO_URL,
                str(pragma_dir),
            ],
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            print(f"  [WARN] agent-pragma clone failed: {clone.stderr.strip()[:300]}")
            return False
    else:
        # Existing checkout -- verify the tag and fetch if needed.
        current = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=str(pragma_dir),
            capture_output=True,
            text=True,
        )
        if current.returncode != 0 or current.stdout.strip() != version:
            print(f"  Updating agent-pragma to {version}...")
            subprocess.run(
                ["git", "fetch", "--tags", "--depth", "1", "origin", version],
                cwd=str(pragma_dir),
                capture_output=True,
            )
            checkout = subprocess.run(
                ["git", "checkout", version],
                cwd=str(pragma_dir),
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                print(
                    f"  [WARN] agent-pragma checkout {version} failed: "
                    f"{checkout.stderr.strip()[:300]}"
                )
                return False
        else:
            print(f"  agent-pragma already at {version}")

    # Run `make install AGENT=opencode PROJECT=<ws>` from the pragma dir.
    print("  Installing agent-pragma into workspace .opencode/...")
    install = subprocess.run(
        ["make", "install", "AGENT=opencode", f"PROJECT={ws_dir}"],
        cwd=str(pragma_dir),
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        print(
            f"  [WARN] agent-pragma install failed: "
            f"{install.stderr.strip()[:400] or install.stdout.strip()[:400]}"
        )
        return False

    print("  agent-pragma installed.")
    # Note: pragma's language-specific linters (ruff/mypy/biome/tsc/
    # golangci-lint) are invoked by the engineer agent using repo
    # runners (`uv run`, `npx`, etc.), not from the global PATH.  We
    # deliberately do NOT check global PATH here -- it produced
    # false-negative warnings for setups that install linters per-repo.
    return True


def cmd_init(args: argparse.Namespace) -> None:
    """Initialise a totomisu workspace.

    Creates the directory structure, copies bundled agent definitions and
    opencode config, clones all ecosystem repos, and writes a global
    config so that ``totomisu run`` works from anywhere.
    """
    print("\n── totomisu init ───────────────────────────────────\n")

    # Check system deps.
    missing = _check_system_deps()
    if missing:
        print(
            f"  [WARN] Missing required tools: {', '.join(missing)}\n"
            f"  Install them before running `totomisu run`.\n"
        )

    # Determine workspace directory.
    if args.directory:
        ws_dir = Path(args.directory).expanduser().resolve()
    else:
        default = Path.cwd() / "totomisu-workspace"
        raw = input(f"  Workspace directory [{default}]: ").strip()
        ws_dir = Path(raw).expanduser().resolve() if raw else default

    print(f"  Workspace: {ws_dir}\n")

    # Create workspace structure.
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "repos").mkdir(exist_ok=True)
    (ws_dir / "specs").mkdir(exist_ok=True)

    # Copy bundled agent definitions.
    pkg_data = get_package_data_path()
    agents_src = pkg_data / "agents"
    agents_dst = ws_dir / ".opencode" / "agents"
    agents_dst.mkdir(parents=True, exist_ok=True)
    for md_file in sorted(agents_src.glob("*.md")):
        shutil.copy2(md_file, agents_dst / md_file.name)
        print(f"  Copied agent: {md_file.name}")

    # Install agent-pragma (skills/commands for deterministic validators).
    # Per-workspace install so the user's global opencode config is untouched.
    _install_agent_pragma(ws_dir, PRAGMA_VERSION)

    # Write opencode.json.
    opencode_cfg = ws_dir / "opencode.json"
    if not opencode_cfg.exists():
        opencode_cfg.write_text(_stock_opencode_json_text())
        print("  Created opencode.json")

    # Write workspace marker.
    marker = ws_dir / WORKSPACE_MARKER
    marker_data = {"version": 1, "workspace": str(ws_dir)}
    marker.write_text(json.dumps(marker_data, indent=2) + "\n")
    print(f"  Created {WORKSPACE_MARKER} marker")

    # Write global config so totomisu can find the workspace from anywhere.
    global_cfg_dir = Path.home() / ".config" / "totomisu"
    global_cfg_dir.mkdir(parents=True, exist_ok=True)
    global_cfg = global_cfg_dir / "config.json"
    global_cfg.write_text(json.dumps({"workspace": str(ws_dir)}, indent=2) + "\n")
    print(f"  Saved global config: {global_cfg}")

    # Clone repos.
    print("\n  Cloning repositories...\n")
    from totomisu.workspace import ensure_repos_cloned

    paths = ProjectPaths(root=ws_dir)
    ensure_repos_cloned(paths)

    # Verify branches.
    print("\n  Verifying default branches...\n")
    for repo in REPOS:
        repo_dir = paths.repo_path(repo.name)
        if repo_dir.exists():
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
            )
            branch = result.stdout.strip() if result.returncode == 0 else "???"
            expected = repo.default_branch
            status = "ok" if branch == expected else f"MISMATCH (expected {expected})"
            print(f"  {repo.name:20s} -> {branch} [{status}]")

    print(f"\n{'=' * 56}")
    print(f"  Workspace ready: {ws_dir}")
    print()
    print(f"  Run from anywhere:")
    print(f"    totomisu run --issue <url>")
    print(f'    totomisu run --prompt "description"')
    print(f"    totomisu dashboard")
    print(f"{'=' * 56}\n")


# ── Update command ───────────────────────────────────────────────────
#
# ``totomisu update`` keeps a workspace in sync with whatever the
# currently-installed totomisu package ships: agent-pragma, bundled
# agent files, and ``opencode.json``.  It never touches cloned repos
# under ``repos/``, nor specs, nor user-modified files.


@dataclass
class _UpdateSummary:
    """Tallies the outcome of a ``totomisu update`` run."""

    pragma_ok: bool = False
    agents_installed: int = 0
    agents_updated: int = 0
    agents_unchanged: int = 0
    agents_user_modified: int = 0
    opencode_json: str = "unchanged"  # unchanged | updated | user-modified | error

    @property
    def ok(self) -> bool:
        return self.pragma_ok and self.opencode_json != "error"


def _refresh_bundled_agents(ws_dir: Path, dry_run: bool) -> _UpdateSummary:
    """Re-copy bundled agent definitions into ``<ws>/.opencode/agents/``.

    Conflict handling: when the workspace copy differs from the package
    version currently on disk, we assume the user edited it locally and
    leave it alone (printing ``skipped (user-modified)``).  The only
    way to overwrite a user-modified file is to delete it first and
    re-run ``update``.
    """
    summary = _UpdateSummary()
    pkg_data = get_package_data_path()
    agents_src = pkg_data / "agents"
    agents_dst = ws_dir / ".opencode" / "agents"

    if not agents_src.exists():
        print("  [WARN] bundled agent directory missing from package data.")
        return summary

    if not dry_run:
        agents_dst.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(agents_src.glob("*.md")):
        dst_file = agents_dst / src_file.name
        src_bytes = src_file.read_bytes()

        if not dst_file.exists():
            if not dry_run:
                dst_file.write_bytes(src_bytes)
            print(f"  installed {src_file.name}")
            summary.agents_installed += 1
            continue

        dst_bytes = dst_file.read_bytes()
        if dst_bytes == src_bytes:
            summary.agents_unchanged += 1
            continue

        # Workspace copy differs from the shipped one.  We cannot tell
        # whether the delta is from a previous totomisu version or a
        # hand-edit, so the safe default is to preserve local content.
        print(f"  skipped {src_file.name} (user-modified)")
        summary.agents_user_modified += 1

    return summary


def _refresh_opencode_json(ws_dir: Path, dry_run: bool) -> str:
    """Rewrite ``opencode.json`` only when it matches the stock default.

    Returns one of ``"unchanged"``, ``"updated"``, ``"user-modified"``,
    ``"error"``.
    """
    opencode_cfg = ws_dir / "opencode.json"
    stock_text = _stock_opencode_json_text()

    if not opencode_cfg.exists():
        if not dry_run:
            try:
                opencode_cfg.write_text(stock_text)
            except OSError as exc:
                print(f"  [WARN] could not write opencode.json: {exc}")
                return "error"
        print("  installed opencode.json")
        return "updated"

    try:
        current = opencode_cfg.read_text()
    except OSError as exc:
        print(f"  [WARN] could not read opencode.json: {exc}")
        return "error"

    if current == stock_text:
        return "unchanged"

    # Try to detect a semantic match even if whitespace drifted.
    try:
        current_data = json.loads(current)
    except json.JSONDecodeError:
        print("  skipped opencode.json (user-modified, invalid JSON)")
        return "user-modified"

    if current_data == _STOCK_OPENCODE_JSON:
        # Same content, different formatting -- rewrite to canonical form.
        if not dry_run:
            try:
                opencode_cfg.write_text(stock_text)
            except OSError as exc:
                print(f"  [WARN] could not write opencode.json: {exc}")
                return "error"
        print("  updated opencode.json (reformatted)")
        return "updated"

    print("  skipped opencode.json (user-modified)")
    return "user-modified"


def cmd_update(args: argparse.Namespace) -> None:
    """Update workspace-scoped totomisu assets in place.

    Refreshes agent-pragma, bundled agent definitions, and the stock
    ``opencode.json`` without re-running ``totomisu init``.  Leaves
    ``repos/``, ``specs/``, the global config, and any user-edited
    files alone.  Exits non-zero on any failure so shell scripts can
    detect problems.
    """
    print("\n── totomisu update ─────────────────────────────────\n")

    paths = get_project_paths()
    ws_dir = paths.root
    dry_run = bool(getattr(args, "dry_run", False))

    if dry_run:
        print("  (dry-run: no files will be modified)\n")

    # Precheck required tools for pragma install.  ``opencode`` itself
    # is not needed by ``update`` (it only copies files and runs
    # ``make install``), so we just warn and continue when it is
    # missing rather than bailing.
    missing = [t for t in ("git", "make") if shutil.which(t) is None]
    if missing:
        print(
            f"  [ERROR] missing required tools: {', '.join(missing)}.\n"
            f"  Install them before running `totomisu update`."
        )
        sys.exit(1)

    summary = _UpdateSummary()

    print(f"  Workspace: {ws_dir}\n")

    # 1. agent-pragma.
    print("  agent-pragma:")
    if dry_run:
        pragma_dir = ws_dir / ".agent-pragma"
        if pragma_dir.exists():
            current = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                cwd=str(pragma_dir),
                capture_output=True,
                text=True,
            )
            tag = current.stdout.strip() if current.returncode == 0 else "unknown"
            print(f"    would reconcile {tag} -> {PRAGMA_VERSION}")
        else:
            print(f"    would clone agent-pragma {PRAGMA_VERSION}")
        summary.pragma_ok = True
    else:
        summary.pragma_ok = _install_agent_pragma(ws_dir, PRAGMA_VERSION)
    print()

    # 2. Bundled agent files.
    print("  bundled agents:")
    agent_summary = _refresh_bundled_agents(ws_dir, dry_run=dry_run)
    summary.agents_installed = agent_summary.agents_installed
    summary.agents_updated = agent_summary.agents_updated
    summary.agents_unchanged = agent_summary.agents_unchanged
    summary.agents_user_modified = agent_summary.agents_user_modified
    print()

    # 3. opencode.json.
    print("  opencode.json:")
    summary.opencode_json = _refresh_opencode_json(ws_dir, dry_run=dry_run)
    print()

    # Summary line.
    print(f"{'=' * 56}")
    print(
        f"  Summary: pragma={'ok' if summary.pragma_ok else 'FAIL'}, "
        f"agents installed={summary.agents_installed} "
        f"unchanged={summary.agents_unchanged} "
        f"user-modified={summary.agents_user_modified}, "
        f"opencode.json={summary.opencode_json}"
    )
    print(f"{'=' * 56}\n")

    if not summary.ok:
        sys.exit(1)


# ── Phase runners ─────────────────────────────────────────────────────


def phase_intake(args: argparse.Namespace) -> TriageResult:
    """Phase 1: Fetch input, classify, confirm."""
    paths = get_project_paths()

    if args.resume:
        print(f"\n  Resuming from slug: {args.resume}")
        triage = load_triage(args.resume, paths)
        if triage is None:
            print(
                f"  [ERROR] No triage found for slug {args.resume!r}.", file=sys.stderr
            )
            sys.exit(1)
        return triage

    if args.issue:
        print("\n── Phase 1: Intake (GitHub issue) ──────────────────")
        print(f"  Fetching: {args.issue}")
        issue = fetch_issue(args.issue)
        input_text = format_issue_as_input(issue)
    else:
        print("\n── Phase 1: Intake (prompt) ─────────────────────────")
        input_text = f"# Feature Request\n\n{args.prompt}"

    print("  Classifying...")
    triage = classify(input_text, paths)
    triage = confirm_triage(triage)

    save_input(triage.slug, input_text, paths)
    save_triage(triage.slug, triage, paths)
    init_status(
        triage.slug, triage.triage_type, triage.repos, paths, spec_phases=triage.phases
    )
    update_phase(triage.slug, "intake", "done", paths)

    print(f"\n  Saved to: specs/{triage.slug}/")
    return triage


def phase_workspace(slug: str, repo_names: list[str]) -> dict[str, Path]:
    """Clone repos and create worktrees."""
    paths = get_project_paths()

    print("\n── Workspace setup ─────────────────────────────────")
    update_phase(slug, "workspace", "running", paths)

    if worktrees_exist(slug, repo_names, paths):
        print("  [update] Worktrees exist, updating to latest upstream...")
        ensure_repos_cloned(paths)
        update_worktrees(slug, repo_names, paths)
        update_phase(slug, "workspace", "done", paths)
        return {name: paths.worktree_path(slug, name) for name in repo_names}

    print("  Ensuring all repos are cloned...")
    ensure_repos_cloned(paths)

    print("  Creating worktrees...")
    worktrees = create_worktrees(slug, repo_names, paths)

    print("  Workspace ready.\n")
    update_phase(slug, "workspace", "done", paths)
    return worktrees


def phase_specs(
    slug: str,
    repo_names: list[str],
    phases: list[str],
    skip_to: str | None,
    *,
    headless: bool = False,
    triage_type: str = "feature",
) -> list[str]:
    """Run the spec-phase agents selected by the triage classifier.

    ``phases`` is a subset of ``["pm", "debate", "designer", "architect"]``.
    Each phase is only executed if it appears in the list.  Phases not in
    the list are marked "skipped" in the status tracker.
    """
    paths = get_project_paths()

    has_pm = "pm" in phases
    has_debate = "debate" in phases
    has_designer = "designer" in phases
    has_architect = "architect" in phases

    # ── PM + Debate ───────────────────────────────────────────────
    if has_pm and headless:
        from totomisu.prd import run_pm_headless

        if _should_skip("pm", skip_to):
            pass
        elif not prd_exists(slug, paths):
            update_phase(slug, "pm", "running", paths)
            run_pm_headless(slug, paths)
            update_phase(slug, "pm", "done", paths)
            # Headless mode auto-critiques, so debate is implicit.
            update_phase(slug, "debate", "done", paths)
        else:
            print(f"  [skip] PRD already exists: specs/{slug}/prd.md")
            update_phase(slug, "pm", "done", paths)
            update_phase(slug, "debate", "done", paths)
    elif has_pm:
        # PM (interactive)
        if _should_skip("pm", skip_to):
            pass
        elif not prd_exists(slug, paths):
            update_phase(slug, "pm", "running", paths)
            run_pm(slug, paths)
            update_phase(slug, "pm", "done", paths)
            _confirm_continue("PM", slug)
        else:
            print(f"  [skip] PRD already exists: specs/{slug}/prd.md")
            update_phase(slug, "pm", "done", paths)

        # Debate (interactive) -- always paired with PM.
        if has_debate:
            if _should_skip("debate", skip_to):
                pass
            elif debate_done(slug, paths):
                print(f"  [skip] Debate already completed for: specs/{slug}/")
                update_phase(slug, "debate", "done", paths)
            elif prd_exists(slug, paths):
                update_phase(slug, "debate", "running", paths)
                run_debate(slug, paths)
                update_phase(slug, "debate", "done", paths)
                _confirm_continue("Debate", slug)
            else:
                update_phase(slug, "debate", "skipped", paths)

    # ── Designer ──────────────────────────────────────────────────
    if has_designer:
        if _should_skip("designer", skip_to):
            pass
        elif not design_exists(slug, paths):
            update_phase(slug, "designer", "running", paths)
            if headless:
                run_designer_headless(slug, paths)
            else:
                run_designer(slug, paths)
            update_phase(slug, "designer", "done", paths)
            if not headless:
                _confirm_continue("Designer", slug)
        else:
            print(f"  [skip] Design doc already exists: specs/{slug}/design.md")
            update_phase(slug, "designer", "done", paths)

    # ── Architect ─────────────────────────────────────────────────
    if has_architect:
        light = triage_type == "complex-bug"
        if _should_skip("architect", skip_to):
            return get_affected_repos(slug, repo_names, paths)
        if not tech_spec_exists(slug, paths):
            update_phase(slug, "architect", "running", paths)
            if headless:
                result = run_architect_headless(slug, repo_names, paths, light=light)
            else:
                result = run_architect(slug, repo_names, paths, light=light)
            update_phase(slug, "architect", "done", paths)
            if not headless:
                _confirm_continue("Architect", slug)
            return result
        print(f"  [skip] Tech spec already exists: specs/{slug}/tech-spec.md")
        update_phase(slug, "architect", "done", paths)

    return get_affected_repos(slug, repo_names, paths)


def phase_build(slug: str, repo_names: list[str]) -> None:
    """Per-repo parallel pipelines: engineer -> review -> PR -> CI."""
    paths = get_project_paths()
    update_phase(slug, "build", "running", paths)
    run_build_pipelines(slug, repo_names, paths)
    update_phase(slug, "build", "done", paths)


def phase_cross_review(slug: str, repo_names: list[str]) -> None:
    """Cross-repo consistency review after all repos finish."""
    paths = get_project_paths()
    if len(repo_names) > 1:
        update_phase(slug, "cross-review", "running", paths)
        run_cross_repo_review(slug, repo_names, paths)
        update_phase(slug, "cross-review", "done", paths)
    else:
        update_phase(slug, "cross-review", "skipped", paths)


def phase_cross_review_fix(slug: str, repo_names: list[str]) -> None:
    """Fix cross-review findings in affected repos (parallel)."""
    paths = get_project_paths()
    cross_review_file = paths.spec_file(slug, "cross-review.md")

    if not cross_review_file.exists():
        print("  [skip] No cross-review file -- nothing to fix.")
        update_phase(slug, "cross-review-fix", "skipped", paths)
        return

    # Quick check: if the review is a clean PASS with no findings, skip.
    content = cross_review_file.read_text(encoding="utf-8")
    if "Summary of Findings" not in content:
        print("  [skip] Cross-review has no findings table.")
        update_phase(slug, "cross-review-fix", "skipped", paths)
        return

    update_phase(slug, "cross-review-fix", "running", paths)
    affected = run_cross_review_fixes(slug, repo_names, paths)

    if affected:
        update_phase(slug, "cross-review-fix", "done", paths)
    else:
        print("  [skip] No repos had actionable findings to fix.")
        update_phase(slug, "cross-review-fix", "skipped", paths)


# ── Run command ───────────────────────────────────────────────────────

_PATH_HEADER = {
    "simple-bug": "Simple Bug",
    "complex-bug": "Complex Bug",
    "feature": "Feature",
}


def _run_ci_check(args: argparse.Namespace) -> None:
    """Standalone CI check for one or all repos."""
    from totomisu.repo_runner import step_ci_watch

    paths = get_project_paths()
    triage = load_triage(args.resume, paths)
    if triage is None:
        print(f"[ERROR] No triage found for slug {args.resume!r}.", file=sys.stderr)
        sys.exit(1)

    slug = triage.slug
    target = args.ci_check
    repos = [target] if target != "all" else triage.repos

    print(f"\n── CI Check ────────────────────────────────────────")
    print(f"  Slug:  {slug}")
    print(f"  Repos: {', '.join(repos)}")
    print("────────────────────────────────────────────────────\n")

    for name in repos:
        wt_path = paths.worktree_path(slug, name)
        if not wt_path.exists():
            print(f"  [{name}] No worktree found, skipping.")
            continue
        step_ci_watch(slug, name, paths)

    save_costs(slug, paths)


def _run_fix_pr(args: argparse.Namespace) -> None:
    """Fetch PR comments and send engineer to fix (parallel via tmux)."""
    from totomisu.engineer import run_fix_pr_pipelines

    paths = get_project_paths()
    triage = load_triage(args.resume, paths)
    if triage is None:
        print(f"[ERROR] No triage found for slug {args.resume!r}.", file=sys.stderr)
        sys.exit(1)

    slug = triage.slug
    target = args.fix_pr
    repos = [target] if target != "all" else triage.repos

    if target != "all" and target not in triage.repos:
        print(
            f"[ERROR] Repo {target!r} not in feature repos: {triage.repos}",
            file=sys.stderr,
        )
        sys.exit(1)

    run_fix_pr_pipelines(slug, repos, paths, attach=True)

    print(f"\n  Done. To re-check CI:")
    print(f"  totomisu run --resume {slug} --ci-check\n")
    save_costs(slug, paths)


def _run_fix_cross_review(args: argparse.Namespace) -> None:
    """Fix cross-review findings for all or a specific repo."""
    paths = get_project_paths()
    triage = load_triage(args.resume, paths)
    if triage is None:
        print(f"[ERROR] No triage found for slug {args.resume!r}.", file=sys.stderr)
        sys.exit(1)

    slug = triage.slug
    target = args.fix_cross_review
    repos = [target] if target != "all" else triage.repos

    print("\n── Fix Cross-Review Findings ────────────────────────")
    print(f"  Slug:  {slug}")
    print(f"  Repos: {', '.join(repos)}")
    print("────────────────────────────────────────────────────\n")

    phase_cross_review_fix(slug, repos)

    print("\n  Done. To re-check CI:")
    print(f"  totomisu run --resume {slug} --ci-check\n")
    save_costs(slug, paths)


def cmd_run(args: argparse.Namespace) -> None:
    """Run the orchestration pipeline."""
    # Handle standalone actions first.
    if args.ci_check is not None:
        if not args.resume:
            print("[ERROR] --ci-check requires --resume.", file=sys.stderr)
            sys.exit(1)
        _run_ci_check(args)
        return

    if args.fix_pr is not None:
        if not args.resume:
            print("[ERROR] --fix-pr requires --resume.", file=sys.stderr)
            sys.exit(1)
        _run_fix_pr(args)
        return

    if args.fix_cross_review is not None:
        if not args.resume:
            print("[ERROR] --fix-cross-review requires --resume.", file=sys.stderr)
            sys.exit(1)
        _run_fix_cross_review(args)
        return

    # Full pipeline.
    skip_to: str | None = getattr(args, "skip_to", None)

    if skip_to and not args.resume:
        print("[ERROR] --skip-to requires --resume.", file=sys.stderr)
        sys.exit(1)

    triage = phase_intake(args)
    slug = triage.slug
    repo_names = triage.repos
    phases_str = ", ".join(triage.phases) if triage.phases else "(none)"

    print(f"\n{'=' * 56}")
    print(f"  Path: {_PATH_HEADER[triage.triage_type]}")
    print(f"  Slug: {slug}")
    print(f"  Repos: {', '.join(repo_names)}")
    print(f"  Phases: {phases_str}")
    if skip_to:
        print(f"  Skip-to: {skip_to}")
    print(f"{'=' * 56}")

    # Workspace: set up early so all agents have repo access.
    if not _should_skip("workspace", skip_to):
        phase_workspace(slug, repo_names)

    # Spec phases: driven by the phases list from intake.
    headless = getattr(args, "headless", False)
    repo_names = phase_specs(
        slug,
        repo_names,
        triage.phases,
        skip_to,
        headless=headless,
        triage_type=triage.triage_type,
    )

    # Cost guardrail: check before the expensive build phase.
    paths = get_project_paths()
    if not check_cost_ceiling(slug, paths):
        print("  Pipeline paused by cost guardrail.")
        print(f"  Resume with: totomisu run --resume {slug} --skip-to build")
        save_costs(slug, paths)
        return

    # Enrich per-repo specs with scope notes and scoping warning.  The spec
    # file is what every agent call attaches via -f, so this is how we get
    # context to agents -- we never write files into the worktree itself,
    # because AGENTS.md and CLAUDE.md are reserved names in opencode.
    if not _should_skip("build", skip_to):
        print("  Enriching per-repo specs...")
        for name in repo_names:
            setup_engineer_context(slug, name, paths)

    # Build: per-repo parallel pipelines.
    if not _should_skip("build", skip_to):
        phase_build(slug, repo_names)

    # Cost guardrail: check after build before cross-review.
    if not check_cost_ceiling(slug, paths):
        print("  Pipeline paused by cost guardrail.")
        print(f"  Resume with: totomisu run --resume {slug} --skip-to cross-review")
        save_costs(slug, paths)
        return

    # Cross-repo review: only sync point.
    if not _should_skip("cross-review", skip_to):
        phase_cross_review(slug, repo_names)

    # Fix cross-review findings (if any).
    if not _should_skip("cross-review-fix", skip_to):
        phase_cross_review_fix(slug, repo_names)

    # Done.
    paths = get_project_paths()
    cost_file = save_costs(slug, paths)
    if cost_file:
        from totomisu.costs import get_feature_costs

        costs = get_feature_costs(slug, paths)
        cost_str = f"${costs['total_cost']:.2f}" if costs else "N/A"
    else:
        cost_str = "N/A (opencode DB not found)"

    print(f"\n{'=' * 56}")
    print(f"  Pipeline complete for: {slug}")
    print(f"  Cost:       {cost_str}")
    print(f"  Specs:      specs/{slug}/")
    print(f"  Worktrees:  specs/{slug}/repos/")
    print(f"  Logs:       specs/{slug}/logs/")
    print(f"  Dashboard:  totomisu dashboard")
    print(f"{'=' * 56}\n")


# ── Pull command ──────────────────────────────────────────────────────


def cmd_pull(args: argparse.Namespace) -> None:
    """Fast-forward every cloned repo's default branch to latest upstream."""
    print("\n── totomisu pull ───────────────────────────────────\n")

    if shutil.which("git") is None:
        print("  [ERROR] git not found on PATH.", file=sys.stderr)
        sys.exit(1)

    paths = get_project_paths()
    print(f"  Workspace: {paths.root}\n")

    results = pull_repos(paths)

    updated = sum(1 for v in results.values() if v == "updated")
    up_to_date = sum(1 for v in results.values() if v == "up-to-date")
    skipped = sum(1 for v in results.values() if v.startswith("skipped"))
    errors = sum(1 for v in results.values() if v.startswith("error"))

    print(f"\n{'=' * 56}")
    print(
        f"  Summary: updated={updated} up-to-date={up_to_date} "
        f"skipped={skipped} errors={errors}"
    )
    print(f"{'=' * 56}\n")

    if errors:
        sys.exit(1)


# ── Reset command ─────────────────────────────────────────────────────


def _prune_worktrees(paths: ProjectPaths) -> None:
    """Detach git worktrees from each clone before wiping ``specs/``.

    Worktrees created under ``specs/<slug>/repos/<repo>`` are registered
    inside their parent clone's ``.git``.  Deleting the worktree
    directories without telling git leaves dangling administrative
    entries.  Removing the parent clone makes that moot, but we prune
    first so a partial failure (e.g. ``repos/`` removed but ``specs/``
    kept) does not leave the surviving clones in an inconsistent state.
    """
    repos_dir = paths.repos_dir
    if not repos_dir.exists():
        return

    for repo in REPOS:
        repo_dir = paths.repo_path(repo.name)
        if not (repo_dir / ".git").exists():
            continue
        # `git worktree prune` only drops stale entries; force-remove each
        # registered worktree first so the directories can be deleted.
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if listing.returncode != 0:
            continue
        main_wt = str(repo_dir.resolve())
        for line in listing.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            wt = line.split(" ", 1)[1].strip()
            if Path(wt).resolve() == Path(main_wt).resolve():
                continue  # never remove the main working tree
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt],
                cwd=str(repo_dir),
                capture_output=True,
            )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_dir),
            capture_output=True,
        )


def cmd_reset(args: argparse.Namespace) -> None:
    """Wipe the workspace's repos and specs, then re-run ``init``.

    Destroys all cloned repos under ``repos/`` and every session under
    ``specs/`` (including their worktrees), then re-runs the init flow
    against the same workspace directory to re-clone everything fresh.
    Workspace-scoped assets (``.opencode/`` agents, ``.agent-pragma``,
    ``opencode.json``, the ``.totomisu`` marker, and the global config)
    are left in place -- init recreates anything missing.
    """
    print("\n── totomisu reset ──────────────────────────────────\n")

    if shutil.which("git") is None:
        print("  [ERROR] git not found on PATH.", file=sys.stderr)
        sys.exit(1)

    paths = get_project_paths()
    ws_dir = paths.root
    print(f"  Workspace: {ws_dir}\n")
    print("  This will DELETE and rebuild:")
    print(f"    - {paths.repos_dir}  (all cloned repos)")
    print(f"    - {paths.specs_dir}  (all sessions and worktrees)")
    print()

    if not args.yes:
        answer = input("  Type 'reset' to confirm: ").strip().lower()
        if answer != "reset":
            print("  Aborted. Nothing was deleted.")
            sys.exit(0)

    # Detach worktrees from their parent clones before deletion.
    print("\n  Pruning worktrees...")
    _prune_worktrees(paths)

    # Wipe specs/ (sessions + worktree dirs) and repos/ (clones).
    for target in (paths.specs_dir, paths.repos_dir):
        if target.exists():
            print(f"  Removing {target}")
            shutil.rmtree(target, ignore_errors=True)

    # Re-run init against the same workspace directory.  Reuse the
    # existing init flow so repos are re-cloned and structure rebuilt.
    print("\n  Re-running init...\n")
    init_args = argparse.Namespace(directory=str(ws_dir))
    cmd_init(init_args)


# ── Dashboard command ─────────────────────────────────────────────────


def cmd_dashboard(args: argparse.Namespace) -> None:
    """Launch the web dashboard."""
    from totomisu.dashboard_server import run_dashboard

    run_dashboard(port=args.port)


# ── Dev commands (otari-ai `make dev`) ────────────────────────────────


def _require_session(slug: str, paths: ProjectPaths) -> None:
    """Exit with a clear error if the session's spec dir does not exist."""
    if not paths.spec_dir(slug).exists():
        print(
            f"[ERROR] No session found for slug {slug!r} "
            f"(missing specs/{slug}/).",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_dev(args: argparse.Namespace) -> None:
    """Run `make dev` for a session's otari-ai worktree."""
    from totomisu.engineer import run_dev_server

    paths = get_project_paths()
    _require_session(args.slug, paths)
    run_dev_server(args.slug, paths)


def cmd_dev_stop(args: argparse.Namespace) -> None:
    """Stop the `make dev` tmux session for a session."""
    from totomisu.engineer import stop_dev_server

    paths = get_project_paths()
    stop_dev_server(args.slug, paths)


def cmd_dev_clean(args: argparse.Namespace) -> None:
    """Run `make clean` in a session's otari-ai worktree."""
    from totomisu.engineer import clean_dev

    paths = get_project_paths()
    _require_session(args.slug, paths)
    clean_dev(args.slug, paths)


# ── Hidden _repo-runner command ───────────────────────────────────────


def cmd_repo_runner(args: argparse.Namespace) -> None:
    """Internal: dispatches repo_runner operations from tmux panes."""
    from totomisu.repo_runner import (
        run_cross_review_fix_pipeline,
        run_fix_pr_pipeline,
        run_repo_pipeline,
        step_ci_watch,
        step_rebase_on_base,
    )
    from totomisu.config import MAX_REVIEW_ROUNDS

    slug = args.slug
    repo = args.repo

    if args.rr_fix_xr:
        run_cross_review_fix_pipeline(slug, repo)
    elif args.rr_fix_pr:
        run_fix_pr_pipeline(slug, repo)
    elif args.rr_ci_check:
        paths = get_project_paths()
        step_ci_watch(slug, repo, paths)
    elif args.rr_rebase:
        paths = get_project_paths()
        step_rebase_on_base(slug, repo, paths)
    else:
        run_repo_pipeline(slug, repo, max_review_rounds=MAX_REVIEW_ROUNDS)


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "init":
        cmd_init(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "pull":
        cmd_pull(args)
    elif args.command == "reset":
        cmd_reset(args)
    elif args.command == "dashboard":
        cmd_dashboard(args)
    elif args.command == "dev":
        cmd_dev(args)
    elif args.command == "dev-stop":
        cmd_dev_stop(args)
    elif args.command == "dev-clean":
        cmd_dev_clean(args)
    elif args.command == "_repo-runner":
        cmd_repo_runner(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
