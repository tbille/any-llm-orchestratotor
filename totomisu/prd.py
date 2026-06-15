"""Phase 2-3.6: Product Manager, PRD debate, and designer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from totomisu.config import (
    CAVEMAN_PROMPT,
    OTARI_UI_DEV_URL,
    ProjectPaths,
    designer_headless_env,
    headless_env,
)


# ── Phase 2: Product Manager (interactive TUI) ───────────────────────


def run_pm(slug: str, paths: ProjectPaths) -> Path:
    """Launch the PM agent in TUI mode to create a PRD.

    The user interacts with the PM until they are satisfied, then exits.
    The PM writes the PRD to ``specs/<slug>/prd.md``.
    """
    spec_dir = paths.spec_dir(slug)
    prd_file = paths.spec_file(slug, "prd.md")

    print("\n── Phase 2: Product Manager ─────────────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  Input:       input.md")
    print(f"  Output:      prd.md")
    print("  The PM agent will open in a TUI session.")
    print("  Collaborate on the PRD, then exit when satisfied.")
    print("────────────────────────────────────────────────────\n")

    prompt = (
        f"You are acting as the Product Manager for this feature.\n\n"
        f"Read the input in input.md (in the current directory) and create a PRD.\n"
        f"Write the PRD to: prd.md (in the current directory)\n\n"
        f"ALL files you need to read or write are in the current directory.\n"
        f"Do NOT access files outside this directory.\n\n"
        f"If anything is unclear, ask me questions before writing.\n"
        f"When you write the PRD, use the template structure from your system prompt."
    )

    subprocess.run(
        [
            "opencode",
            "--agent",
            "product-manager",
            "--prompt",
            prompt,
            str(spec_dir),
        ],
        cwd=str(spec_dir),
    )

    if not prd_file.exists():
        print(f"[WARN] PRD not found at {prd_file}.", file=sys.stderr)
        print("       The PM session may not have written it yet.")
        answer = input("       Continue anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit(1)

    return prd_file


# ── Phase 2+3 combined: Headless PM + self-critique ───────────────────


def run_pm_headless(slug: str, paths: ProjectPaths) -> Path:
    """Run PM and debate as a single headless agent call.

    The agent writes the PRD, critiques it, and revises it in one pass.
    No user interaction required.  Produces the same ``prd.md`` output
    as the interactive flow.
    """
    spec_dir = paths.spec_dir(slug)
    prd_file = paths.spec_file(slug, "prd.md")
    input_file = paths.spec_file(slug, "input.md")

    print("\n── Phase 2+3: Headless PM + Debate ─────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  Input:       input.md")
    print(f"  Output:      prd.md")
    print("  Running headless (no TUI interaction)...")
    print("────────────────────────────────────────────────────\n")

    prompt = (
        f"You are acting as both the Product Manager and Reviewer.\n\n"
        f"## Working directory scope\n"
        f"ALL files you read or write are in the CURRENT WORKING DIRECTORY.\n"
        f"You have access to:\n"
        f"- `input.md` (the feature request)\n"
        f"- `triage.json` (initial classification)\n"
        f"- `repos/<repo-name>/` (per-spec worktrees of affected repositories,\n"
        f"   available INSIDE the current directory). You MAY read these to\n"
        f"   inform the PRD.\n"
        f"You MUST NOT access any path outside the current directory.  Do\n"
        f"NOT use `../`, absolute paths, or parent-directory references.\n"
        f"The ecosystem's upstream clones live elsewhere on disk and are\n"
        f"OFF LIMITS; only the `repos/` inside the current dir is yours.\n\n"
        f"## Task\n"
        f"Read the input document and create a comprehensive PRD.\n"
        f"Then critically review your own PRD for:\n"
        f"- Missing edge cases\n"
        f"- Cross-repo consistency gaps\n"
        f"- Backwards compatibility issues\n"
        f"- Scope creep\n"
        f"- Missing acceptance criteria\n\n"
        f"Revise the PRD to address any issues found.\n"
        f"Write the final PRD to: prd.md (in the current directory)\n\n"
        f"Use the PRD template from your system prompt (the full version with\n"
        f"functional/non-functional requirements and cross-repo impact analysis).\n"
        f"You are running non-interactively: do NOT ask questions -- record any\n"
        f"unknowns under the 'Open Questions' section instead."
    )

    file_args: list[str] = []
    if input_file.exists():
        file_args = ["-f", str(input_file)]

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
    )

    if result.returncode != 0:
        print(
            f"  [WARN] Headless PM agent exited with code {result.returncode}",
            file=sys.stderr,
        )

    if not prd_file.exists():
        print(f"  [WARN] PRD not found at {prd_file}.", file=sys.stderr)
        print("         The headless PM may not have written it.")

    # Write debate-done marker since critique was included.
    from datetime import datetime, timezone

    marker = paths.spec_file(slug, "debate-done")
    marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")

    return prd_file


# ── Phase 3: PRD Debate (interactive TUI) ─────────────────────────────


def run_debate(slug: str, paths: ProjectPaths) -> Path:
    """Launch the reviewer agent to critique and refine the PRD.

    The user can participate in the debate. The PRD is refined in place.
    Writes a ``debate-done`` marker file on completion so that
    ``--resume`` does not re-launch the debate TUI.
    """
    spec_dir = paths.spec_dir(slug)
    prd_file = paths.spec_file(slug, "prd.md")

    print("\n── Phase 3: PRD Debate ──────────────────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  PRD:         prd.md")
    print("  The reviewer will critique the PRD.")
    print("  You can participate in the discussion.")
    print("  Exit when the PRD is satisfactory.")
    print("────────────────────────────────────────────────────\n")

    prompt = (
        f"Review the PRD at prd.md (in the current directory).\n\n"
        f"ALL files you need to read or write are in the current directory.\n"
        f"Do NOT access files outside this directory.\n\n"
        f"Critique it thoroughly: check for missing edge cases, cross-repo "
        f"consistency, backwards compatibility, scope creep, and missing "
        f"acceptance criteria.\n\n"
        f"After the discussion, update prd.md in place with improvements."
    )

    subprocess.run(
        [
            "opencode",
            "--agent",
            "reviewer",
            "--prompt",
            prompt,
            str(spec_dir),
        ],
        cwd=str(spec_dir),
    )

    # Write marker file so --resume skips the debate.
    from datetime import datetime, timezone

    marker = paths.spec_file(slug, "debate-done")
    marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")

    return prd_file


# ── Phase 3.6: Product Designer (interactive TUI, conditional) ────────


def run_designer(slug: str, paths: ProjectPaths) -> Path:
    """Launch the designer agent to create design proposals.

    Only called when ``"designer"`` is in the triage phases list.
    """
    spec_dir = paths.spec_dir(slug)
    design_file = paths.spec_file(slug, "design.md")

    print("\n── Phase 3.6: Product Designer ──────────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  PRD:         prd.md")
    print(f"  Output:      design.md")
    print(f"  UI dev URL:  {OTARI_UI_DEV_URL}")
    print("  The designer will create UX/DX proposals.")
    print("  For otari-ai UI features, start the dev server first:")
    print(f"    totomisu dev {slug}")
    print("  so the designer can navigate the live UI with Playwright.")
    print("  Collaborate with the designer, then exit.")
    print("────────────────────────────────────────────────────\n")

    prompt = (
        f"Read the PRD at prd.md (in the current directory).\n\n"
        f"ALL files you read or write are in the CURRENT WORKING DIRECTORY.\n"
        f"Do NOT access files outside this directory. Do NOT use `../`,\n"
        f"absolute paths, or parent-directory references. The ecosystem's\n"
        f"upstream clones live elsewhere on disk and are OFF LIMITS; only the\n"
        f"`repos/` inside the current dir is yours.\n\n"
        f"Create design proposals covering:\n"
        f"- User/developer flows and interactions\n"
        f"- SDK API ergonomics (method names, signatures, return types)\n"
        f"- Error handling UX\n"
        f"- CLI or configuration changes (if applicable)\n"
        f"- Documentation patterns\n\n"
        f"## Live UI (otari-ai web-UI features only)\n"
        f"If this feature touches the otari-ai web UI, use the Playwright\n"
        f"browser tool to navigate the running dev server at {OTARI_UI_DEV_URL}\n"
        f"and ground your visual/interaction design in the existing screens and\n"
        f"components. If the URL is not reachable, proceed text-only and note\n"
        f"that you could not inspect the live UI. Skip the browser entirely for\n"
        f"pure SDK/CLI/gateway/backend features.\n\n"
        f"If the feature has no developer-facing surface, say so and keep the\n"
        f"design minimal rather than inventing scope.\n\n"
        f"Write the design document to: design.md (in the current directory)"
    )

    subprocess.run(
        [
            "opencode",
            "--agent",
            "designer",
            "--prompt",
            prompt,
            str(spec_dir),
        ],
        cwd=str(spec_dir),
    )

    return design_file


# ── Phase 3.6: Product Designer (headless) ────────────────────────────


def _ui_dev_server_reachable(url: str, timeout: float = 2.0) -> bool:
    """Return ``True`` if the otari-ai UI dev server answers at ``url``.

    Used to decide whether the headless designer should attempt live-UI
    navigation via Playwright.  Any HTTP response (even a 4xx/5xx) counts
    as reachable; only connection failures / timeouts count as down.  We
    never auto-start ``make dev`` -- the user starts it with
    ``totomisu dev <slug>`` when they want UI grounding.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310
            return True
    except urllib.error.HTTPError:
        # Server responded with an error status -- it is up.
        return True
    except (urllib.error.URLError, OSError):
        return False


def run_designer_headless(slug: str, paths: ProjectPaths) -> Path:
    """Run the designer agent as a single headless pass.

    Produces the same ``design.md`` output as the interactive flow.
    No user interaction required.
    """
    spec_dir = paths.spec_dir(slug)
    design_file = paths.spec_file(slug, "design.md")
    prd_file = paths.spec_file(slug, "prd.md")

    ui_up = _ui_dev_server_reachable(OTARI_UI_DEV_URL)

    print("\n── Phase 3.6: Designer (headless) ──────────────────")
    print(f"  Working dir: {spec_dir}")
    print(f"  PRD:         prd.md")
    print(f"  Output:      design.md")
    print(f"  UI dev URL:  {OTARI_UI_DEV_URL} ({'up' if ui_up else 'not reachable'})")
    if not ui_up:
        print(f"  (start it with `totomisu dev {slug}` for live-UI grounding)")
    print("  Running headless (no TUI interaction)...")
    print("────────────────────────────────────────────────────\n")

    if ui_up:
        ui_block = (
            f"## Live UI (otari-ai web-UI features only)\n"
            f"The otari-ai web UI dev server is RUNNING at {OTARI_UI_DEV_URL}.\n"
            f"If this feature touches that web UI, use the Playwright browser\n"
            f"tool to navigate it and ground your visual/interaction design in\n"
            f"the existing screens and components (reuse existing patterns and\n"
            f"naming). The Playwright browser is the ONLY exception to the\n"
            f"working-directory scope rule above: it may reach {OTARI_UI_DEV_URL}\n"
            f"and use its own browser data dirs. Save any screenshots into the\n"
            f"current directory. Skip the browser entirely for pure\n"
            f"SDK/CLI/gateway/backend features.\n\n"
        )
    else:
        ui_block = (
            f"## Live UI\n"
            f"The otari-ai web UI dev server is NOT reachable at\n"
            f"{OTARI_UI_DEV_URL}. Do not attempt browser navigation. If this is\n"
            f"a web-UI feature, design text-only and note that the live UI could\n"
            f"not be inspected.\n\n"
        )

    prompt = (
        f"{CAVEMAN_PROMPT}"
        f"Read the PRD at prd.md (in the current directory).\n\n"
        f"## Working directory scope\n"
        f"ALL files you read or write are in the CURRENT WORKING DIRECTORY.\n"
        f"You have access to:\n"
        f"- `prd.md` (the product requirements document)\n"
        f"- `input.md`, `triage.json`\n"
        f"- `repos/<repo-name>/` (per-spec worktrees of affected repositories,\n"
        f"   available INSIDE the current directory). You MAY read these to\n"
        f"   ground your design in existing SDK patterns.\n"
        f"You MUST NOT access any path outside the current directory (the\n"
        f"Playwright browser tool below is the only exception).  Do NOT use\n"
        f"`../`, absolute paths, or parent-directory references for file\n"
        f"access. The ecosystem's upstream clones live elsewhere on disk and\n"
        f"are OFF LIMITS; only the `repos/` inside the current dir is yours.\n\n"
        f"{ui_block}"
        f"## Task\n"
        f"Create design proposals covering:\n"
        f"- User/developer flows and interactions\n"
        f"- SDK API ergonomics (method names, signatures, return types)\n"
        f"- Error handling UX\n"
        f"- CLI or configuration changes (if applicable)\n"
        f"- Documentation patterns\n\n"
        f"If the feature has no developer-facing surface, say so and keep the\n"
        f"design minimal rather than inventing scope.\n\n"
        f"Write the design document to: design.md (in the current directory)"
    )

    file_args: list[str] = []
    if prd_file.exists():
        file_args = ["-f", str(prd_file)]

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
        env=designer_headless_env(),
    )

    if result.returncode != 0:
        print(
            f"  [WARN] Headless designer agent exited with code {result.returncode}",
            file=sys.stderr,
        )

    if not design_file.exists():
        print(f"  [WARN] Design doc not found at {design_file}.", file=sys.stderr)
        print("         The headless designer may not have written it.")

    return design_file


# ── Convenience: check if phases already completed ────────────────────


def prd_exists(slug: str, paths: ProjectPaths) -> bool:
    return paths.spec_file(slug, "prd.md").exists()


def debate_done(slug: str, paths: ProjectPaths) -> bool:
    """Check if the debate phase has already completed."""
    return paths.spec_file(slug, "debate-done").exists()


def design_exists(slug: str, paths: ProjectPaths) -> bool:
    return paths.spec_file(slug, "design.md").exists()
