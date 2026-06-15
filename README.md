<p align="center">
  <img src="icon.png" alt="totomisu" width="400">
</p>

# totomisu

Multi-repo orchestrator for the [otari ecosystem](https://github.com/mozilla-ai). Coordinates feature work, bug fixes, and cross-repo changes across all seven repositories from a single entry point.

## Repositories

| Repo | Language | Role |
|------|----------|------|
| [any-llm](https://github.com/mozilla-ai/any-llm) | Python | LLM-interaction library -- common interface for LLM calls; used by the otari gateway |
| [otari](https://github.com/mozilla-ai/otari) | Python | Gateway service -- routes LLM requests, captures observability |
| [otari-sdk-python](https://github.com/mozilla-ai/otari-sdk-python) | Python | Python SDK -- talks to the gateway |
| [otari-sdk-rust](https://github.com/mozilla-ai/otari-sdk-rust) | Rust | Rust SDK -- talks to the gateway |
| [otari-sdk-go](https://github.com/mozilla-ai/otari-sdk-go) | Go | Go SDK -- talks to the gateway |
| [otari-sdk-ts](https://github.com/mozilla-ai/otari-sdk-ts) | TypeScript | TypeScript SDK -- talks to the gateway |
| [otari-ai](https://github.com/mozilla-ai/otari-ai) | Python | Platform -- budgets, users, observability |

## Installation

Requires Python 3.12+. macOS/Linux only.

```sh
# With uv (recommended)
uv pip install .

# Or with pip
pip install .

# Or as a global tool (no venv activation needed)
uv tool install .
```

This installs the `totomisu` command on your PATH.

### System dependencies

The following tools must be installed and available on PATH:

| Tool | Required | Purpose |
|------|----------|---------|
| [opencode](https://opencode.ai) | Yes | AI coding agent CLI |
| [gh](https://cli.github.com/) | Yes | GitHub CLI (must be authenticated) |
| git | Yes | Version control |
| [tmux](https://github.com/tmux/tmux) | Yes | Terminal multiplexer for parallel pipelines |
| [uv](https://docs.astral.sh/uv/) | Optional | Used by individual repo test commands |
| [wt](https://worktrunk.dev/) | Optional | Git worktree helper (falls back to `git worktree add`) |

## Getting started

### 1. Initialise a workspace

```sh
totomisu init
```

This will:
- Ask you where the workspace should be created (or pass a directory: `totomisu init ~/my-workspace`)
- Create the `repos/` and `specs/` directories
- Clone all seven ecosystem repos
- Copy the bundled agent definitions into the workspace
- Write a global config so `totomisu` works from anywhere

### 2. Run the orchestrator

```sh
# From a GitHub issue
totomisu run --issue https://github.com/mozilla-ai/otari-sdk-python/issues/123

# From a free-form prompt
totomisu run --prompt "Add batch API support to all SDKs"

# Headless mode (PM and debate run non-interactively)
totomisu run --prompt "Add batch API support to all SDKs" --headless
```

### 3. Resume, check, and fix

```sh
# Resume a previous run
totomisu run --resume add-batch-api

# Skip to a specific phase
totomisu run --resume add-batch-api --skip-to build

# Check CI status for all repos (or a specific one)
totomisu run --resume add-batch-api --ci-check
totomisu run --resume add-batch-api --ci-check otari-sdk-python

# Fix PR review comments
totomisu run --resume add-batch-api --fix-pr
totomisu run --resume add-batch-api --fix-pr otari

# Fix cross-review findings
totomisu run --resume add-batch-api --fix-cross-review
totomisu run --resume add-batch-api --fix-cross-review otari-sdk-ts
```

### otari-ai dev server

Run the `otari-ai` dev server (`make dev`) from a session's worktree
(`specs/<slug>/repos/otari-ai`). The server runs in a tmux session that stays
alive when you detach (Ctrl-B D); stop it explicitly with `dev-stop`.

```sh
# Launch `make dev` in tmux and attach (re-attaches if already running)
totomisu dev add-batch-api

# Kill the `make dev` tmux session
totomisu dev-stop add-batch-api

# Run `make clean` (stops a running dev session first)
totomisu dev-clean add-batch-api
```

### CLI flags

| Flag | Description |
|------|-------------|
| `--issue URL` | GitHub issue URL to work on |
| `--prompt TEXT` | Free-form prompt describing the work |
| `--resume SLUG` | Resume a previous run from its slug |
| `--skip-to PHASE` | Skip to a specific phase (requires `--resume`). Choices: `intake`, `workspace`, `pm`, `debate`, `designer`, `architect`, `build`, `cross-review`, `cross-review-fix` |
| `--headless` | Run PM and debate phases non-interactively. The PRD is generated and self-critiqued in a single pass. |
| `--ci-check [REPO]` | Check CI status for all repos or a specific repo (requires `--resume`) |
| `--fix-pr [REPO]` | Fetch PR review comments and send engineer to fix (requires `--resume`) |
| `--fix-cross-review [REPO]` | Fix cross-review findings for all affected repos or a specific one (requires `--resume`) |

## How it works

The orchestrator triages the input and routes it through a dynamic set of spec phases based on the nature of the work. The intake classifier selects which phases to run -- purely technical features skip PM/debate/designer, simple bugs go straight to build, and so on.

```
                                           Workspace setup
                                           (clone + worktrees)
                                                   │
                         ┌─ simple-bug ────────────┼──────────────────┐
                         │                         │                  │
Input ─> Triage ─────────┼─ complex-bug ──> Architect (light) ───────┤
                         │                         │                  │
                         └─ feature ──> PM* ──> Debate* ──> Designer?┤
                                                   ──> Architect     │
                                                                     v
                                              Build (per-repo, parallel)
                              ┌──────────────────┼──────────────────┐
                              v                  v                  v
                      otari-sdk-python       otari        otari-sdk-ts
                        ┌──────────┐       ┌──────────┐      ┌──────────┐
                        │ engineer │       │ engineer │      │ engineer │
                        │ test     │       │ test     │      │ test     │
                        │ review   │       │ review   │      │ review   │
                        │ PR       │       │ PR       │      │ PR       │
                        │ CI watch │       │ CI watch │      │ CI watch │
                        └────┬─────┘       └────┬─────┘      └────┬─────┘
                             └──────────────────┼─────────────────┘
                                                v
                                       Cross-repo review
```

*\* The intake classifier chooses which spec phases to run. For features, the default is all four (PM, debate, designer, architect). Technical features may skip PM/debate/designer. The classifier's recommendation can be overridden at the confirmation prompt.*

Workspace runs right after triage so that all subsequent agents have the repo code available under `specs/<slug>/repos/`. Each repo flows through its own build pipeline independently -- no waiting for other repos.

### Phases

| Phase | Mode | What happens |
|-------|------|-------------|
| **Intake + Triage** | Headless | Fetches the issue via `gh`, classifies as simple-bug / complex-bug / feature, and selects which spec phases to run. You confirm or override. |
| **Workspace** | Automated | Runs right after triage. Clones missing repos, creates git worktrees (via `wt` or `git worktree add` fallback), rebases onto the latest base branch. |
| **Product Manager** | Interactive TUI (or headless with `--headless`) | Creates a PRD. Asks clarifying questions if needed. Skipped if the classifier deems it unnecessary. |
| **Debate** | Interactive TUI (or headless with `--headless`) | A reviewer agent critiques the PRD. You participate until satisfied. Skipped if PM is skipped. |
| **Designer** | Interactive TUI (conditional) | Creates UX/DX proposals if the feature has user-facing impact. |
| **Architect** | Interactive TUI | Creates tech spec with shared interface contracts and per-repo specs. |
| **Build** | Parallel tmux panes | One pane per repo. Each runs: engineer -> targeted tests -> code review -> fix loop -> PR -> CI watch + fix. If code review doesn't pass after `MAX_REVIEW_ROUNDS` (default 2), a draft PR is created. |
| **Cross-review** | Headless | After all repos finish, checks cross-repo interface alignment using the full feature-branch diffs. |

### Context isolation

Each engineer agent runs in its own worktree directory with only its per-repo spec. It never sees other repos' code or specs. The architect's shared interface contract is copied into each per-repo spec so engineers can build independently without a massive shared context window.

### Build pipeline details

Each per-repo build pipeline includes several notable behaviors:

- **Targeted test execution**: Instead of running the full test suite, the pipeline detects which files changed on the feature branch and maps them to language-specific test targets (e.g., `test_module.py` for Python, `cargo test <module>` for Rust, `go test ./pkg/...` for Go, `*.test.ts` for TypeScript). Falls back to the full suite if targeted detection fails.
- **Pre-review build check**: Targeted tests run before the code review step to avoid wasting a review cycle on broken code. If tests fail, the engineer gets one immediate retry with the failure output.
- **Automatic rebase**: Before every push, the pipeline rebases onto the latest base branch. If conflicts arise, an agent attempts to resolve them (up to 5 rounds).
- **Draft PRs**: If code review doesn't pass after the configured number of review rounds, a draft PR is created instead of a regular one.
- **Simple bug investigation**: For simple bugs (no spec file), a headless investigation step scans the repo and writes a brief note identifying the likely root cause before the engineer begins.
- **Deterministic PR creation**: PRs are created via shell commands first. An AI agent is only invoked as a fallback when a PR template needs filling.

## Project structure

```
totomisu/                           # Python package (pip install .)
├── cli.py                          # CLI entry point (init, run, dashboard, _repo-runner)
├── config.py                       # Repo registry, paths, env-var tunables
├── intake.py                       # Issue fetching, triage classifier
├── parse.py                        # Structured output parsing (JSON, verdicts, findings)
├── prd.py                          # PM, debate, designer phases
├── architect.py                    # Tech spec generation
├── workspace.py                    # Repo cloning, worktree creation
├── engineer.py                     # Tmux launcher, cross-repo review, fix pipelines
├── repo_runner.py                  # Per-repo pipeline: engineer -> test -> review -> PR -> CI
├── pr.py                           # PR creation (deterministic + AI fallback), CI helpers
├── costs.py                        # Cost/token aggregation from opencode's SQLite DB
├── status.py                       # Concurrent-safe status tracking (fcntl.flock)
├── dashboard_server.py             # Web dashboard HTTP server
└── data/
    ├── agents/                     # Bundled agent definitions
    │   ├── product-manager.md
    │   ├── reviewer.md
    │   ├── designer.md
    │   ├── architect.md
    │   ├── code-reviewer.md
    │   └── pr-creator.md
    └── dashboard/                  # Bundled frontend assets
        ├── index.html
        ├── docs.html
        ├── css/
        └── js/
```

After `totomisu init`, the workspace contains:

```
<workspace>/
├── .totomisu                       # Workspace marker (JSON)
├── opencode.json                   # opencode config
├── .opencode/agents/               # Agent definitions (copied from package)
├── repos/                          # Cloned upstream repos
│   ├── any-llm/
│   ├── otari/
│   ├── otari-sdk-python/
│   ├── otari-sdk-rust/
│   ├── otari-sdk-go/
│   ├── otari-sdk-ts/
│   └── otari-ai/
└── specs/                          # Per-feature workspaces (created during runs)
    └── <slug>/
        ├── input.md
        ├── triage.json
        ├── prd.md
        ├── design.md
        ├── tech-spec.md
        ├── <repo>-spec.md
        ├── <repo>-review.md
        ├── cross-review.md
        ├── status.json
        ├── costs.json
        ├── repos/                  # Git worktrees
        └── logs/                   # Agent output logs
```

## Dashboard

Monitor and control all active features from a browser:

```sh
totomisu dashboard               # http://localhost:8080
totomisu dashboard --port 9090   # custom port
```

The dashboard auto-refreshes and shows:

- **Phase progress** per feature (color coded: green=done, blue=running, gray=pending)
- **Per-repo status** within the active phase, with step history timeline
- **Real-time log streaming** per repo via Server-Sent Events (click to expand)
- **Document viewer** for specs, reviews, PRDs, and other artifacts (grouped by category)
- **Cost tracking** per feature with per-repo and per-phase breakdowns
- **PR links and CI status** with `needs_rebase` detection
- **Active tmux sessions**

### Dashboard actions

The dashboard provides buttons to trigger operations without using the CLI:

| Action | What it does |
|--------|-------------|
| **Fix PRs** | Fetches PR review comments and launches engineer fix pipelines (all repos or a specific one) |
| **Stop Fixing** | Kills running fix-pr tmux sessions |
| **CI Check** | Triggers a CI status check for a specific repo |
| **Resume** | Resumes a paused pipeline from a specific phase |
| **Rebase** | Rebases a repo's worktree onto the latest base branch |
| **Cancel** | Kills all running tmux sessions for a feature and marks running phases as failed |

## Running multiple features in parallel

Each feature is fully isolated by slug -- separate spec directories, worktrees, tmux sessions, and branches. You can run multiple orchestrators simultaneously:

```sh
# Terminal 1: complete interactive phases for Feature A
totomisu run --issue https://github.com/mozilla-ai/otari-sdk-python/issues/123

# Terminal 2: start Feature B once A's interactive phases are done
totomisu run --prompt "Add rate limiting to the gateway"

# Terminal 3: dashboard watches both
totomisu dashboard
```

To re-run only the build phase (e.g. after editing a spec manually):

```sh
totomisu run --resume add-batch-api --skip-to build
```

File locking on `repos/` ensures parallel orchestrators don't corrupt shared git state during clone or worktree creation.

## Workspace resolution

`totomisu run` and `totomisu dashboard` find the workspace automatically:

1. `$TOTOMISU_WORKSPACE` environment variable (if set)
2. Walk up from the current directory looking for a `.totomisu` marker file
3. Read from `~/.config/totomisu/config.json` (written by `totomisu init`)

## Resumability

Every phase writes its output to `specs/<slug>/`. If a phase's output already exists, it is skipped on re-run. Use `--resume <slug>` to pick up where you left off.

## Cost tracking

The orchestrator reads opencode's SQLite database to track costs and token usage across all phases. Cost data is saved to `specs/<slug>/costs.json` and displayed in the dashboard.

A configurable cost guardrail pauses the pipeline before expensive phases if the accumulated cost exceeds the ceiling. You are prompted to continue or abort.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TOTOMISU_WORKSPACE` | (none) | Override workspace path |
| `ORCHESTRATOR_MAX_REVIEW_ROUNDS` | 2 | Engineer -> review -> fix cycles before creating a PR |
| `ORCHESTRATOR_MAX_CI_FIX_ROUNDS` | 2 | CI failure -> fix -> re-push cycles |
| `ORCHESTRATOR_CI_POLL_INTERVAL` | 30 | Seconds between CI status polls |
| `ORCHESTRATOR_CLASSIFIER_TIMEOUT` | 120 | Seconds before a headless classifier call times out |
| `ORCHESTRATOR_COST_CEILING` | 200.0 | USD cost ceiling before the pipeline pauses for confirmation |
| `ORCHESTRATOR_BUILD_PHASE_TIMEOUT` | 5400 | Build phase tmux wait timeout (seconds, default 90 min) |
