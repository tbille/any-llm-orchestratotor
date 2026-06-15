# AGENTS.md

Multi-repo orchestrator for the otari ecosystem. Coordinates feature work across 7 repositories using AI agents via opencode. Zero Python dependencies — everything is stdlib. Packaged as `totomisu`.

## System requirements

All must be on PATH: `opencode`, `gh` (authenticated), `git`, `tmux`.
Optional: `uv` (used by individual repo test commands), `wt` (worktrunk) — falls back to `git worktree add`, `make` (used by `totomisu init` to install agent-pragma), `npx`/Node (used by the Playwright MCP server that lets the designer navigate the otari-ai web UI; first run downloads `@playwright/mcp` and browser binaries).
agent-pragma's linters (`ruff`, `mypy`/`ty`, `biome`, `tsc`, `golangci-lint`) are invoked by the engineer agent via per-repo runners (`uv run`, `npx`, or whatever each ecosystem repo already uses). No global install is required as long as each repo's normal dev toolchain is set up.

Python 3.12 (pinned in `.python-version`). macOS/Linux only — uses `fcntl.flock()`.

## Installation

```sh
pip install .          # or: uv pip install .
```

This installs the `totomisu` command on PATH.

## Running

```sh
totomisu init                                         # set up workspace (clones repos, creates dirs)
totomisu update                                       # refresh pragma + bundled agents + opencode.json
totomisu update --dry-run                             # preview what would change
totomisu pull                                         # fast-forward all repo clones to latest
totomisu reset                                        # wipe repos/ + specs/ and re-run init (prompts to confirm)
totomisu reset -y                                     # same, skipping the confirmation prompt
totomisu run --issue <github-issue-url>
totomisu run --prompt "description"
totomisu run --prompt "description" --headless        # no TUI interaction for any phase
totomisu run --resume <slug>
totomisu run --resume <slug> --skip-to engineer
totomisu run --resume <slug> --ci-check all
totomisu run --resume <slug> --fix-pr all
totomisu dev <slug>                                   # run `make dev` (otari-ai) in tmux
totomisu dev-stop <slug>                              # kill the `make dev` tmux session
totomisu dev-clean <slug>                             # run `make clean` (stops dev first)
totomisu dashboard                                    # http://localhost:8080
```

There are no build, test, lint, format, or typecheck commands for this repo itself. No CI pipeline exists. No Makefile, pre-commit, or test suite.

## Architecture

- `totomisu/cli.py` — CLI entry point with subcommands: `init`, `update`, `run`, `pull`, `reset`, `dashboard`, `dev`, `dev-stop`, `dev-clean`, `_repo-runner` (hidden, for tmux panes). The `pull` command fast-forwards every cloned repo's default branch to latest upstream (`pull_repos` in `workspace.py`); it is non-destructive and skips repos with uncommitted changes, a non-default branch checked out, or local divergence. The `reset` command (`cmd_reset`) is the destructive counterpart: it prunes all git worktrees from each clone (`_prune_worktrees`), deletes `repos/` and `specs/` wholesale, then re-invokes `cmd_init` against the same workspace to re-clone everything. It prompts for a typed `reset` confirmation unless `-y`/`--yes` is passed, and preserves workspace-scoped assets (`.opencode/`, `.agent-pragma`, `opencode.json`, the `.totomisu` marker, global config). The `init` command sets up a workspace directory with repos, specs, and agent definitions. The `update` command re-runs the in-place install steps (agent-pragma, bundled agent files, `opencode.json`) without re-cloning repos; preserves user-modified agent files and exits non-zero on failure. The `run` command orchestrates the pipeline. The `dev`/`dev-stop`/`dev-clean` commands operate on a session's `otari-ai` worktree (`specs/<slug>/repos/otari-ai`): `dev` launches `make dev` in a tmux session (`dev-<slug>`) left alive on detach, `dev-stop` kills that session, and `dev-clean` stops it then runs `make clean` (foreground); the launchers live in `engineer.py` (`run_dev_server`, `stop_dev_server`, `clean_dev`). Workspace resolution: `$TOTOMISU_WORKSPACE` env → walk up from cwd for `.totomisu` marker → `~/.config/totomisu/config.json`.
- `totomisu/dashboard_server.py` — Standalone HTTP server. Frontend assets bundled in `totomisu/data/dashboard/`.
- `totomisu/config.py` — Repo registry, env-var tunables, path helpers. Source of truth for repo metadata. Each `RepoInfo` now includes `test_command` for pre-review build checks. `get_project_paths()` resolves the workspace root. `get_package_data_path()` locates bundled assets.
- `totomisu/intake.py` — Fetches issues via `gh`, classifies via opencode headless mode. The classifier returns a `phases` list (subset of pm, debate, designer, architect) that controls which spec agents run. Parses opencode's `--format json` output as newline-delimited JSON events.
- `totomisu/parse.py` — Structured output parsing for agent responses. Extracts JSON blocks, review verdicts, and cross-review repo lists. Replaces ad-hoc string matching.
- `totomisu/workspace.py` — Clones repos to `repos/`, creates worktrees under `specs/<slug>/repos/`. Before the build phase, enriches each per-repo spec file (`specs/<slug>/<repo>-spec.md`) in place with a scoping warning, scope notes, and test hints. Nothing is written into the worktree itself: `AGENTS.md` and `CLAUDE.md` are reserved names that opencode auto-loads as project rules, so injecting orchestrator content there would pollute PRs and clash with upstream rule files. Agents receive context via `-f` attachments of the enriched spec.
- `totomisu/engineer.py` — Launches per-repo pipelines as tmux panes. Each pane runs `totomisu _repo-runner <slug> <repo>`. Build phase has a configurable timeout.
- `totomisu/repo_runner.py` — Per-repo pipeline module. Runs in tmux panes via the hidden `_repo-runner` CLI subcommand. Includes pre-review build check and simple bug investigation steps.
- `totomisu/status.py` — Concurrent-safe status tracking via `fcntl.flock()` and atomic write-then-rename to `status.json`.
- `totomisu/pr.py` — Tries deterministic PR creation first (shell commands only). Falls back to AI agent only when a PR template needs filling.
- `totomisu/costs.py` — Reads opencode's SQLite DB directly for cost/token aggregation.

## Key directories (workspace)

After `totomisu init`, the workspace contains:
- `repos/` — Cloned upstream repos
- `specs/<slug>/` — Per-feature workspace: specs, reviews, status.json, costs.json, `repos/` (worktrees), `logs/`
- `.opencode/agents/` — Six agent definitions (copied from package data during init)
- `.opencode/commands/` + `.opencode/skills/` — agent-pragma commands and skills (populated by `make install AGENT=opencode` during init)
- `.agent-pragma/` — Pinned checkout of `peteski22/agent-pragma` (version from `PRAGMA_VERSION`, default `3.2.3`)
- `.totomisu` — Workspace marker file (JSON with version and path)

## Key directories (package)

- `totomisu/data/agents/` — Bundled agent definition .md files
- `totomisu/data/dashboard/` — Bundled frontend assets (HTML/CSS/JS)

## Non-obvious behaviors

- **Workspace resolution**: `get_project_paths()` checks: (1) `$TOTOMISU_WORKSPACE` env var, (2) walk up from cwd for `.totomisu` marker, (3) `~/.config/totomisu/config.json`.
- **Resumability**: Each phase writes output to `specs/<slug>/`. If the output file exists, the phase is skipped on re-run.
- **Cost guardrail**: Pipeline pauses before expensive phases if accumulated cost exceeds `$ORCHESTRATOR_COST_CEILING` (default $200).
- **Draft PRs**: If code review doesn't pass after `MAX_REVIEW_ROUNDS` (default 2), a draft PR is created instead.
- **Context isolation**: Each engineer agent runs in its own worktree with only its per-repo spec. It never sees other repos' code.
- **`otari-sdk-python` scope trap**: The `otari-sdk-python` repo contains gateway *client* code (in scope) but gateway *server* code lives in the `otari` repo. Agents are explicitly warned not to add server code to `otari-sdk-python`.
- **CAVEMAN_PROMPT** in `totomisu/config.py` is applied to headless agent calls for token savings. It includes the instruction to never create AGENTS.md files unless asked.
- **Designer live-UI navigation**: The workspace `opencode.json` registers a Playwright MCP server (`@playwright/mcp` via `npx`) so the designer agent can navigate the running otari-ai web UI and keep visual/interaction designs consistent with existing screens. It is gated to otari-ai web-UI features; for SDK/CLI/backend work the designer is told to skip the browser. The headless designer (`run_designer_headless` in `prd.py`) probes `OTARI_UI_DEV_URL` (`_ui_dev_server_reachable`) and only enables live navigation when the dev server is up — it never auto-starts `make dev`; the user runs `totomisu dev <slug>` first. The headless designer uses `designer_headless_env()` (not `headless_env()`): it relaxes the `external_directory` permission to `allow` so Playwright's headless browser doesn't deadlock on an unanswerable permission prompt, whereas PM/architect keep the strict deny sandbox.
- **agent-pragma enforcement**: `totomisu init` clones `peteski22/agent-pragma` (pinned tag, per-workspace) and runs `make install AGENT=opencode PROJECT=<workspace>`. The per-repo pipeline runs `/validate` between build-check and review (`step_pragma_validate` in `repo_runner.py`). HARD violations feed back into the engineer fix-loop using the same mechanism as build failures; a `<repo>-pragma-violations.md` file is attached on the next fix round. The full `/validate` report is persisted at `specs/<slug>/<repo>-pragma-report.md` for debugging. Each `RepoInfo` has a `pragma_validators` tuple that picks which validators run (language-specific + universal). `otari-sdk-rust` gets only universal validators because agent-pragma has no Rust-specific validator.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TOTOMISU_WORKSPACE` | (none) | Override workspace path |
| `ORCHESTRATOR_MAX_REVIEW_ROUNDS` | 2 | Engineer → review → fix cycles before PR |
| `ORCHESTRATOR_MAX_CI_FIX_ROUNDS` | 2 | CI fail → fix → re-push cycles |
| `ORCHESTRATOR_CI_POLL_INTERVAL` | 30 | Seconds between CI polls |
| `ORCHESTRATOR_CLASSIFIER_TIMEOUT` | 120 | Headless classifier timeout (seconds) |
| `ORCHESTRATOR_COST_CEILING` | 200.0 | USD cost ceiling before pipeline pauses |
| `ORCHESTRATOR_BUILD_PHASE_TIMEOUT` | 5400 | Build phase tmux wait timeout (seconds, default 90 min) |
| `PRAGMA_ENABLED` | `1` | Set to `0` to disable the pragma `/validate` step |
| `PRAGMA_VERSION` | `3.2.3` | agent-pragma git tag checked out during `totomisu init` |
| `PRAGMA_VALIDATE_TIMEOUT` | 300 | Seconds before `/validate` is considered timed out (non-blocking) |
| `OTARI_UI_DEV_URL` | `http://localhost:5181` | Base URL of the otari-ai web UI dev server the designer navigates via Playwright |

## Code conventions

- `from __future__ import annotations` in every file
- Type hints throughout, `Path` objects (not strings) for filesystem paths
- Section headers use `# ── Name ──────` box-drawing style
- f-strings exclusively for formatting
- `subprocess.run` with `capture_output=True`, errors to stderr
