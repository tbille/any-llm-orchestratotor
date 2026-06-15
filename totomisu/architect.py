"""Phase 4: Technical Architect -- creates tech spec and per-repo implementation specs."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from totomisu.config import CAVEMAN_PROMPT, REPO_BY_NAME, ProjectPaths, headless_env

# Matches the architect's final ``AFFECTED_REPOS: a, b, c`` line.
_AFFECTED_REPOS_RE = re.compile(
    r"^\s*AFFECTED_REPOS\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _build_architect_prompt(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
    *,
    light: bool,
) -> tuple[str, list[str]]:
    """Build the architect prompt and the list of existing context files.

    Returns:
        Tuple of (prompt, context_files). ``context_files`` is the list of
        relative filenames (prd.md/design.md/input.md) that actually exist
        in the spec directory; useful for logging.
    """
    spec_dir = paths.spec_dir(slug)

    # Check which context files exist (using relative names).
    context_files: list[str] = []
    for name in ("prd.md", "design.md", "input.md"):
        if (spec_dir / name).exists():
            context_files.append(name)

    repo_lines: list[str] = []
    for name in repo_names:
        if name not in REPO_BY_NAME:
            continue
        info = REPO_BY_NAME[name]
        line = f"- **{name}** ({info.language}): {info.description}"
        if info.scope_notes:
            line += f"\n  - **Scope note:** {info.scope_notes}"
        if info.test_hints:
            line += f"\n  - **Test hints:** {info.test_hints}"
        repo_lines.append(line)
    repo_descriptions = "\n".join(repo_lines)

    # The canonical filenames the orchestrator looks for.  The architect MUST
    # use these EXACT names -- the affected-repo list is derived from which of
    # these files exist, so a misnamed file silently drops a repo.
    spec_filenames = ", ".join(f"`{name}-spec.md`" for name in repo_names)

    if light:
        task_instruction = (
            "This is a **complex bug** that needs a focused investigation and fix plan.\n"
            "Create a lightweight technical spec that covers:\n"
            "1. Root cause hypothesis\n"
            "2. Which repos need changes and why\n"
            "3. The fix approach for each repo\n"
            "4. Shared interfaces or contracts that must remain consistent\n"
            "5. Testing strategy\n"
        )
    else:
        task_instruction = (
            "This is a **new feature** that needs a full technical specification.\n"
            "Create a tech spec that covers:\n"
            "1. Architecture overview\n"
            "2. Shared interfaces / API contracts that multiple repos must agree on\n"
            "3. Per-repo implementation plan (what each repo needs to do)\n"
            "4. Dependency order (which repo changes must land first)\n"
            "5. Migration / backwards compatibility strategy\n"
            "6. Testing strategy\n"
        )

    # List which worktrees are available.
    worktree_listing = "\n".join(
        f"- `repos/{name}/` ({REPO_BY_NAME[name].language})"
        for name in repo_names
        if name in REPO_BY_NAME and (spec_dir / "repos" / name).exists()
    )

    prompt = (
        f"You are the Technical Architect for this work.\n\n"
        f"## Working directory scope\n"
        f"ALL files you read or write are in the CURRENT WORKING DIRECTORY.\n"
        f"You MUST NOT access any path outside the current directory.  Do\n"
        f"NOT use `../`, absolute paths, or parent-directory references.\n"
        f"The ecosystem's upstream clones live elsewhere on disk and are\n"
        f"OFF LIMITS; only the `repos/` inside the current dir is yours.\n\n"
        f"## Repository code\n"
        f"The source code for each affected repository is available as a\n"
        f"worktree at `repos/<repo-name>/` **inside the current directory**.\n"
        f"Use relative paths only (e.g. `repos/otari-sdk-python/src/...`).  Browse\n"
        f"the code to understand existing APIs, types, and patterns:\n"
        f"{worktree_listing}\n\n"
        f"Use these to inform your specs -- check existing interfaces,\n"
        f"naming conventions, and test patterns before designing new ones.\n\n"
        f"## Context files (in the current directory)\n"
        f"Read these files for the full context:\n"
        + "\n".join(f"- {f}" for f in context_files)
        + f"\n\n"
        f"## Affected repositories (initial assessment)\n"
        f"{repo_descriptions}\n\n"
        f"You may add or remove repos from this list if your analysis shows different needs.\n"
        f"**Scope trap:** the gateway *server* lives ONLY in `otari`. The\n"
        f"`otari-sdk-python` repo is the Python *client* SDK that talks to the\n"
        f"gateway. The `any-llm` repo is the LLM-interaction *library* the gateway\n"
        f"imports to reach providers -- it is neither the server nor the client SDK.\n"
        f"Never put server changes in an SDK, and never confuse `any-llm` with `otari-sdk-python`.\n\n"
        f"## Design handoff\n"
        f"If `design.md` is present, treat its naming and API shapes as authoritative\n"
        f"unless they are technically infeasible -- in which case note the deviation and\n"
        f"why. You own the precise contract, versioning, and failure modes; the designer\n"
        f"owns ergonomics. Reconcile, do not silently override.\n\n"
        f"## Task\n"
        f"{task_instruction}\n"
        f"Write the overall tech spec to: tech-spec.md\n\n"
        f"Additionally, for EACH affected repo, write a standalone implementation spec.\n"
        f"You MUST use these EXACT filenames (one per affected repo), in the current\n"
        f"directory: {spec_filenames}\n"
        f"If you add a repo, name its spec `<canonical-repo-name>-spec.md` using the\n"
        f"repo names shown above. If you drop a repo, do not create its spec file.\n\n"
        f"Each per-repo spec should be self-contained: an engineer reading ONLY that file "
        f"(plus the shared interface section) should know exactly what to build.\n\n"
        f"For targeted tests, prefer the per-repo test hints shown above. You do not need\n"
        f"to enumerate exact test paths the orchestrator will inject them; focus on WHAT\n"
        f"to test rather than the precise command.\n\n"
        f"IMPORTANT: The FINAL line of your response MUST be exactly:\n"
        f"AFFECTED_REPOS: repo1, repo2, repo3\n"
        f"listing the canonical names of every repo you wrote a spec for. The orchestrator\n"
        f"reads this line to confirm the final set."
    )

    return prompt, context_files


def run_architect(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
    *,
    light: bool = False,
) -> list[str]:
    """Launch the architect agent to produce technical specifications.

    Args:
        slug: Feature slug.
        repo_names: Repos identified by triage as affected.
        paths: Project paths.
        light: If True, produce a lighter investigation-focused spec
               (used for complex-bug path instead of full feature spec).

    Returns:
        The final list of affected repo names (the architect may adjust it).
    """
    spec_dir = paths.spec_dir(slug)
    mode_label = (
        "lightweight investigation spec" if light else "full technical specification"
    )

    prompt, context_files = _build_architect_prompt(
        slug, repo_names, paths, light=light
    )

    print(f"\n── Phase 4: Architect ({mode_label}) ────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  Context:     {', '.join(context_files)}")
    print(f"  Output:      tech-spec.md + per-repo specs")
    print(f"  Repos:       {', '.join(repo_names)}")
    print("  The architect agent will open in a TUI session.")
    print("  Collaborate on the tech spec, then exit.")
    print("────────────────────────────────────────────────────\n")

    subprocess.run(
        [
            "opencode",
            "--agent",
            "architect",
            "--prompt",
            prompt,
            str(spec_dir),
        ],
        cwd=str(spec_dir),
    )

    # Try to determine the final repo list from the tech spec.
    return _extract_affected_repos(slug, repo_names, paths)


def run_architect_headless(
    slug: str,
    repo_names: list[str],
    paths: ProjectPaths,
    *,
    light: bool = False,
) -> list[str]:
    """Run the architect agent as a single headless pass.

    Produces the same ``tech-spec.md`` + per-repo specs as the interactive
    flow.  No user interaction required.
    """
    spec_dir = paths.spec_dir(slug)
    tech_spec_file = paths.spec_file(slug, "tech-spec.md")
    mode_label = (
        "lightweight investigation spec" if light else "full technical specification"
    )

    prompt, context_files = _build_architect_prompt(
        slug, repo_names, paths, light=light
    )
    prompt = CAVEMAN_PROMPT + prompt

    print(f"\n── Phase 4: Architect headless ({mode_label}) ──────")
    print(f"  Working dir: {spec_dir}")
    print(f"  Context:     {', '.join(context_files)}")
    print(f"  Output:      tech-spec.md + per-repo specs")
    print(f"  Repos:       {', '.join(repo_names)}")
    print("  Running headless (no TUI interaction)...")
    print("────────────────────────────────────────────────────\n")

    # Pass the context files via -f so opencode attaches them directly.
    file_args: list[str] = []
    for name in context_files:
        file_args.extend(["-f", str(spec_dir / name)])

    result = subprocess.run(
        [
            "opencode",
            "run",
            "--dir",
            str(spec_dir),
            "--dangerously-skip-permissions",
            *file_args,
            "--",
            prompt,
        ],
        cwd=str(spec_dir),
        env=headless_env(),
        capture_output=True,
        text=True,
    )

    # Mirror the agent's output so the TUI/log still shows it.
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if result.returncode != 0:
        print(
            f"  [WARN] Headless architect agent exited with code {result.returncode}",
            file=sys.stderr,
        )

    if not tech_spec_file.exists():
        print(f"  [WARN] Tech spec not found at {tech_spec_file}.", file=sys.stderr)
        print("         The headless architect may not have written it.")

    return _extract_affected_repos(
        slug, repo_names, paths, agent_output=result.stdout or ""
    )


def _parse_affected_repos_line(agent_output: str) -> list[str]:
    """Extract the canonical repo names from the architect's AFFECTED_REPOS line.

    Returns the repos declared on the last ``AFFECTED_REPOS:`` line, filtered to
    names the orchestrator knows about.  Returns an empty list if no valid line
    is present.
    """
    if not agent_output:
        return []

    matches = _AFFECTED_REPOS_RE.findall(agent_output)
    if not matches:
        return []

    # Use the last occurrence in case the agent echoed the template earlier.
    declared = [part.strip() for part in matches[-1].split(",")]
    return [name for name in declared if name in REPO_BY_NAME]


def _extract_affected_repos(
    slug: str,
    fallback_repos: list[str],
    paths: ProjectPaths,
    *,
    agent_output: str = "",
) -> list[str]:
    """Determine which repos the architect targeted.

    Source of truth is the per-repo spec files that were actually written
    (``<repo>-spec.md``).  When the agent's ``AFFECTED_REPOS:`` line is
    available, it is cross-checked against those files and any mismatch is
    surfaced as a warning so misnamed/missing specs are not silently dropped.
    """
    spec_dir = paths.spec_dir(slug)
    found: list[str] = [
        name for name in REPO_BY_NAME if (spec_dir / f"{name}-spec.md").exists()
    ]

    declared = _parse_affected_repos_line(agent_output)
    if declared:
        missing_specs = [name for name in declared if name not in found]
        extra_specs = [name for name in found if name not in declared]
        if missing_specs:
            print(
                "  [WARN] Architect declared AFFECTED_REPOS "
                f"{missing_specs} but no matching <repo>-spec.md was written. "
                "These repos will be DROPPED. Check for a misnamed spec file.",
                file=sys.stderr,
            )
        if extra_specs:
            print(
                f"  [WARN] Spec files exist for {extra_specs} but they are not "
                "in the architect's AFFECTED_REPOS line. Including them anyway.",
                file=sys.stderr,
            )

    if found:
        return found

    # Fallback to the triage list if no per-repo specs were written yet.
    return fallback_repos


# ── Resume helpers ────────────────────────────────────────────────────


def tech_spec_exists(slug: str, paths: ProjectPaths) -> bool:
    return paths.spec_file(slug, "tech-spec.md").exists()


def get_affected_repos(
    slug: str, fallback: list[str], paths: ProjectPaths
) -> list[str]:
    """Return the list of repos that have per-repo specs (or fallback)."""
    return _extract_affected_repos(slug, fallback, paths)
