"""Repository registry and path configuration for the otari ecosystem."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


# ── Workspace marker ─────────────────────────────────────────────────
# Written by ``totomisu init`` in the workspace root.
WORKSPACE_MARKER = ".totomisu"


@dataclass(frozen=True)
class RepoInfo:
    """Metadata for a single repository in the ecosystem."""

    name: str
    github_url: str
    language: str
    description: str
    default_branch: str = "main"
    scope_notes: str = ""
    test_hints: str = ""
    test_command: str = ""
    """Shell command to run the **full** test suite.  Used as fallback by
    the build-check step and still the canonical CI command."""
    targeted_test_command: str = ""
    """Shell template for running only tests affected by the current
    changes.  Must contain ``{targets}`` which will be replaced with
    the language-specific list of test files / packages / modules
    identified from ``git diff``.  When empty, the build-check step
    falls back to *test_command*."""
    pragma_validators: tuple[str, ...] = ()
    """agent-pragma validators to enforce for this repo.  Empty means
    pragma validation is skipped.  Typical values: ``security``,
    ``state-machine``, ``error-handling`` plus one language-specific
    validator (``python-style``, ``go-effective``, ``go-proverbs``,
    ``typescript-style``).  Rust repos only get the universal trio
    because agent-pragma has no Rust-specific validator."""

    @property
    def github_slug(self) -> str:
        """Return 'org/repo' from the full URL."""
        return "/".join(self.github_url.rstrip("/").split("/")[-2:])


# ── Repository registry ──────────────────────────────────────────────

REPOS: tuple[RepoInfo, ...] = (
    RepoInfo(
        name="any-llm",
        github_url="https://github.com/mozilla-ai/any-llm",
        language="python",
        description=(
            "Python library providing a common interface for LLM calls. "
            "Used by the otari gateway to talk to LLM providers, and "
            "supports direct provider calls as well."
        ),
        scope_notes=(
            "This repo is the LLM-interaction library that the otari gateway "
            "uses internally to reach providers. It is NOT the otari gateway "
            "server and NOT the otari Python SDK. Do NOT add gateway server "
            "code here (that lives in 'otari') and do NOT add gateway client "
            "SDK code here (that lives in 'otari-sdk-python'). Only the "
            "provider-interaction library code lives here."
        ),
        test_hints=(
            "NEVER run the full test suite (e.g. `uv run pytest tests/unit` "
            "or `uv run pytest`). The full suite is slow and runs in CI. "
            "Run ONLY the specific test files related to your changes: "
            "uv run pytest tests/unit/<relevant_test_file> -x -q. "
            "Do NOT run integration tests. "
            "For linting use: uv run ruff check . && uv run mypy."
        ),
        test_command="uv run pytest tests/unit -x -q --timeout=60",
        targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "python-style",
        ),
    ),
    RepoInfo(
        name="otari",
        github_url="https://github.com/mozilla-ai/otari",
        language="python",
        description=(
            "LLM gateway service. Routes requests through the any-llm "
            "library to various LLM providers. Captures observability data."
        ),
        test_hints=(
            "NEVER run the full test suite (e.g. `uv run pytest` without a "
            "specific path). The full suite is slow and runs in CI. "
            "Run ONLY the specific test files related to your changes: "
            "uv run pytest tests/<relevant_test_file> -x -q. "
            "For linting: uv run ruff check . && uv run mypy."
        ),
        test_command="uv run pytest -x -q --timeout=60",
        targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "python-style",
        ),
    ),
    RepoInfo(
        name="otari-sdk-python",
        github_url="https://github.com/mozilla-ai/otari-sdk-python",
        language="python",
        description="Python SDK for communicating with the otari gateway.",
        test_hints=(
            "NEVER run the full test suite (e.g. `uv run pytest` without a "
            "specific path). The full suite is slow and runs in CI. "
            "Run ONLY the specific test files related to your changes: "
            "uv run pytest tests/<relevant_test_file> -x -q. "
            "For linting: uv run ruff check . && uv run mypy."
        ),
        test_command="uv run pytest -x -q --timeout=60",
        targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "python-style",
        ),
    ),
    RepoInfo(
        name="otari-sdk-rust",
        github_url="https://github.com/mozilla-ai/otari-sdk-rust",
        language="rust",
        description="Rust SDK for communicating with the otari gateway.",
        test_hints=(
            "NEVER run the full test suite (e.g. `cargo test --all-features` "
            "without a filter). The full suite is slow and runs in CI. "
            "Run ONLY the tests related to your changes: "
            "cargo test <test_name_or_module> --all-features. "
            "Lint: cargo clippy --all-features -- -D warnings && cargo fmt --check."
        ),
        test_command="cargo test --all-features",
        targeted_test_command="cargo test {targets} --all-features",
        # agent-pragma has no Rust validator -- universal trio only.
        pragma_validators=("security", "state-machine", "error-handling"),
    ),
    RepoInfo(
        name="otari-sdk-go",
        github_url="https://github.com/mozilla-ai/otari-sdk-go",
        language="go",
        description="Go SDK for communicating with the otari gateway.",
        test_hints=(
            "NEVER run the full test suite (e.g. `go test ./...`). "
            "The full suite is slow and runs in CI. "
            "Run ONLY the tests in packages you changed: "
            "go test ./path/to/package -race -count=1. "
            "Lint: golangci-lint run."
        ),
        test_command="go test ./... -race -count=1",
        targeted_test_command="go test {targets} -race -count=1",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "go-effective",
            "go-proverbs",
        ),
    ),
    RepoInfo(
        name="otari-sdk-ts",
        github_url="https://github.com/mozilla-ai/otari-sdk-ts",
        language="typescript",
        description="TypeScript SDK for communicating with the otari gateway.",
        test_hints=(
            "NEVER run the full test suite (e.g. `npm test` without args). "
            "The full suite is slow and runs in CI. "
            "Run ONLY the tests related to your changes. Check package.json "
            "for the test runner (jest/vitest) and pass the relevant test "
            "file paths. "
            "Lint: npx biome check . or the lint script in package.json."
        ),
        test_command="npm test",
        targeted_test_command="npx vitest run {targets}",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "typescript-style",
        ),
    ),
    RepoInfo(
        name="otari-ai",
        github_url="https://github.com/mozilla-ai/otari-ai",
        language="python",
        description=(
            "Managed platform for budgets, users, and observability. "
            "Pulls observability data from the gateway."
        ),
        test_hints=(
            "NEVER run the full test suite (e.g. `uv run pytest` without a "
            "specific path). The full suite is slow and runs in CI. "
            "Run ONLY the specific test files related to your changes: "
            "uv run pytest tests/<relevant_test_file> -x -q. "
            "For linting: uv run ruff check . && uv run mypy."
        ),
        test_command="uv run pytest -x -q --timeout=60",
        targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
        pragma_validators=(
            "security",
            "state-machine",
            "error-handling",
            "python-style",
        ),
    ),
)

REPO_BY_NAME: dict[str, RepoInfo] = {r.name: r for r in REPOS}

# ── Ecosystem context (shared with all agents) ───────────────────────

ECOSYSTEM_CONTEXT = """\
# otari Ecosystem

## Repositories and relationships

| Repo | Language | Role |
|------|----------|------|
| any-llm | Python | LLM-interaction library -- common interface for LLM calls; used by the otari gateway to reach providers, and supports direct provider calls |
| otari | Python | Gateway service -- routes LLM requests via the any-llm library, captures observability data |
| otari-sdk-python | Python | Python SDK -- client that talks to the otari gateway |
| otari-sdk-rust | Rust | Rust SDK -- talks to the gateway |
| otari-sdk-go | Go | Go SDK -- talks to the gateway |
| otari-sdk-ts | TypeScript | TypeScript SDK -- talks to the gateway |
| otari-ai | Python | Managed platform -- budgets, users, observability; pulls data from the gateway |

## Dependency graph

```
otari-ai --> otari --> any-llm --> providers (OpenAI, Anthropic, etc.)
otari-sdk-python -----> otari
otari-sdk-rust -------> otari
otari-sdk-go ---------> otari
otari-sdk-ts -> otari
```

## Key facts
- `any-llm` is the LLM-interaction library: the otari gateway imports it to reach providers; it also supports direct provider calls.
- The Python, Rust, Go, and TypeScript SDKs (otari-sdk-*) are clients that talk to the otari gateway over HTTP.
- The platform sits on top and manages budgets/users/observability by querying the gateway.
- Changes to the gateway API surface affect ALL SDKs.
- Changes to the any-llm library can affect the gateway (which imports it).
"""

# ── Pipeline tunables ─────────────────────────────────────────────────
# All values can be overridden via environment variables.


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


MAX_REVIEW_ROUNDS: int = _env_int("ORCHESTRATOR_MAX_REVIEW_ROUNDS", 2)
"""Maximum engineer -> review -> fix cycles before proceeding to PR."""

MAX_CI_FIX_ROUNDS: int = _env_int("ORCHESTRATOR_MAX_CI_FIX_ROUNDS", 2)
"""Maximum CI failure -> fix -> re-push cycles."""

CI_POLL_INTERVAL: int = _env_int("ORCHESTRATOR_CI_POLL_INTERVAL", 30)
"""Seconds between CI status polls."""

CLASSIFIER_TIMEOUT: int = _env_int("ORCHESTRATOR_CLASSIFIER_TIMEOUT", 120)
"""Seconds before a headless classifier call is considered timed out."""

BUILD_PHASE_TIMEOUT: int = _env_int("ORCHESTRATOR_BUILD_PHASE_TIMEOUT", 5400)
"""Seconds before the build phase tmux wait times out (default 90 min)."""

OTARI_UI_DEV_URL: str = os.environ.get("OTARI_UI_DEV_URL", "http://localhost:5181")
"""Base URL of the running otari-ai web UI dev server.  The designer
agent navigates this with Playwright (when reachable) to ground visual
and interaction designs in existing screens.  The default matches the
otari-ai frontend dev port; override via ``OTARI_UI_DEV_URL`` if the
repo's dev server binds elsewhere."""


# ── agent-pragma integration ─────────────────────────────────────────
#
# agent-pragma (https://github.com/peteski22/agent-pragma) provides
# deterministic validators that run as opencode skills/commands.  We
# install it per-workspace during ``totomisu init`` and then invoke
# ``/validate`` between the engineer and review steps so HARD
# violations feed back into the existing fix-loop.

PRAGMA_REPO_URL: str = "https://github.com/peteski22/agent-pragma.git"
"""Upstream agent-pragma repo.  Cloned shallowly into the workspace."""

PRAGMA_DEFAULT_VERSION: str = "3.2.3"
"""Pinned release tag.  Bump intentionally to track new validators."""

PRAGMA_VERSION: str = os.environ.get("PRAGMA_VERSION", PRAGMA_DEFAULT_VERSION)
"""Effective pragma tag.  Override via ``PRAGMA_VERSION`` env var."""

PRAGMA_ENABLED: bool = os.environ.get("PRAGMA_ENABLED", "1") != "0"
"""Toggle pragma validation off by exporting ``PRAGMA_ENABLED=0``."""

PRAGMA_VALIDATE_TIMEOUT: int = _env_int("PRAGMA_VALIDATE_TIMEOUT", 300)
"""Seconds before a ``/validate`` run is considered timed out."""


# ── Caveman prompt (token-saving mode for headless agents) ────────────

CAVEMAN_PROMPT = (
    "Terse like caveman. Technical substance exact. Only fluff die. "
    "Drop: articles, filler (just/really/basically), pleasantries, hedging. "
    "Fragments OK. Short synonyms. Code unchanged. "
    "Pattern: [thing] [action] [reason]. [next step]. "
    "ACTIVE EVERY RESPONSE. No revert after many turns. No filler drift. "
    "Code/commits/PRs: write normal. "
    "NEVER create an AGENTS.md file in the repository unless explicitly "
    "asked to do so. "
)


# ── Headless permission sandbox ───────────────────────────────────────
#
# Inline opencode config injected via ``OPENCODE_CONFIG_CONTENT`` for
# every headless spec-phase agent call (PM, designer, architect).  It
# confines the agent to its session working directory by denying any
# ``external_directory`` access.
#
# Rationale: opencode's default permission ruleset sets
# ``external_directory`` to ``"ask"``.  In headless mode (``opencode
# run``) there is no UI to answer the prompt, so the session deadlocks
# indefinitely on any tool call touching a path outside its cwd --
# which is exactly what happens when an explore/research subagent
# wanders into ``/Users/.../totomisu-workspace/repos/`` (the upstream
# clones) instead of the worktrees inside ``specs/<slug>/repos/``.
#
# ``--dangerously-skip-permissions`` on the CLI does NOT cover
# ``external_directory``; that gate is evaluated separately.  The
# object-form ``{"*": "deny"}`` rule below catches every path outside
# the session cwd while letting opencode's own built-in allow rules for
# its tool-output cache and skills directories win by specificity.
HEADLESS_SANDBOX_CONFIG_JSON: str = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"external_directory": {"*": "deny"}},
    }
)


def headless_env() -> dict[str, str]:
    """Return ``os.environ`` copy with the sandbox config injected.

    Use as ``env=headless_env()`` when calling ``subprocess.run`` to
    spawn a headless ``opencode run`` for spec phases (PM, architect).
    Ensures the child session cannot wander outside its own working
    directory and hang on unanswerable permission prompts.

    The designer phase uses ``designer_headless_env()`` instead because
    its Playwright MCP browsing needs filesystem access outside the
    session cwd (browser profile + tool-output dirs).
    """
    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = HEADLESS_SANDBOX_CONFIG_JSON
    return env


# The designer may navigate a running web UI via the Playwright MCP.
# That MCP launches a headless browser whose profile and tool-output
# directories live outside the session cwd, so the strict
# ``{"*": "deny"}`` external-directory rule would deadlock the headless
# run on an unanswerable permission prompt.  Relax that single gate to
# ``"allow"`` for the designer while keeping the rest of the sandbox.
# (Network access to ``localhost`` is not gated by ``external_directory``.)
DESIGNER_SANDBOX_CONFIG_JSON: str = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"external_directory": {"*": "allow"}},
    }
)


def designer_headless_env() -> dict[str, str]:
    """Return ``os.environ`` copy with the designer sandbox injected.

    Use as ``env=designer_headless_env()`` for the headless designer
    pass.  Unlike :func:`headless_env`, it permits external-directory
    access so the Playwright MCP can drive a headless browser without
    deadlocking on an unanswerable permission prompt.
    """
    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = DESIGNER_SANDBOX_CONFIG_JSON
    return env


# ── Path helpers ──────────────────────────────────────────────────────


@dataclass
class ProjectPaths:
    """All paths derived from the project root."""

    root: Path

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def specs_dir(self) -> Path:
        return self.root / "specs"

    @property
    def agents_dir(self) -> Path:
        return self.root / ".opencode" / "agents"

    @property
    def pragma_dir(self) -> Path:
        """Workspace-local checkout of agent-pragma."""
        return self.root / ".agent-pragma"

    def repo_path(self, repo_name: str) -> Path:
        return self.repos_dir / repo_name

    def spec_dir(self, slug: str) -> Path:
        return self.specs_dir / slug

    def spec_file(self, slug: str, filename: str) -> Path:
        return self.spec_dir(slug) / filename

    def worktree_dir(self, slug: str) -> Path:
        return self.spec_dir(slug) / "repos"

    def worktree_path(self, slug: str, repo_name: str) -> Path:
        return self.worktree_dir(slug) / repo_name

    def logs_dir(self, slug: str) -> Path:
        return self.spec_dir(slug) / "logs"

    def ensure_spec_dirs(self, slug: str) -> None:
        """Create the full directory tree for a spec."""
        self.spec_dir(slug).mkdir(parents=True, exist_ok=True)
        self.worktree_dir(slug).mkdir(parents=True, exist_ok=True)
        self.logs_dir(slug).mkdir(parents=True, exist_ok=True)


def _find_workspace_root() -> Path | None:
    """Walk up from cwd looking for the ``.totomisu`` marker file."""
    cur = Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        if (parent / WORKSPACE_MARKER).exists():
            return parent
    return None


def _read_global_config() -> Path | None:
    """Read the workspace path from ``~/.config/totomisu/config.json``."""
    cfg = Path.home() / ".config" / "totomisu" / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
            ws = Path(data["workspace"])
            if (ws / WORKSPACE_MARKER).exists():
                return ws
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return None


def get_project_paths() -> ProjectPaths:
    """Return paths rooted at the active workspace.

    Resolution order:
      1. ``$TOTOMISU_WORKSPACE`` env var
      2. Walk up from cwd looking for ``.totomisu`` marker
      3. ``~/.config/totomisu/config.json``
    """
    # 1. Env var
    env_ws = os.environ.get("TOTOMISU_WORKSPACE")
    if env_ws:
        root = Path(env_ws).resolve()
        if (root / WORKSPACE_MARKER).exists():
            return ProjectPaths(root=root)

    # 2. Walk up from cwd
    root = _find_workspace_root()
    if root is not None:
        return ProjectPaths(root=root)

    # 3. Global config
    root = _read_global_config()
    if root is not None:
        return ProjectPaths(root=root)

    # Fallback: error with guidance.
    raise SystemExit(
        "[ERROR] No totomisu workspace found.\n"
        "Run `totomisu init` to create one, or set $TOTOMISU_WORKSPACE."
    )


def get_package_data_path() -> Path:
    """Return the path to bundled package data (agents, dashboard assets).

    Uses ``importlib.resources`` to locate the ``totomisu/data`` directory,
    falling back to a ``__file__``-relative path for editable installs.
    """
    try:
        ref = resources.files("totomisu") / "data"
        # Materialise to a real path (works for both installed and editable).
        return Path(str(ref))
    except (TypeError, FileNotFoundError):
        return Path(__file__).resolve().parent / "data"
