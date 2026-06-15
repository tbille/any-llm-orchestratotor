# Plan: Targeted Test Execution in Build Check

## Problem

`step_build_check` in `lib/repo_runner.py` always runs the **full** test suite via `test_command`, which is slow for large repos. The full suite should be deferred to CI (already handled by `step_ci_watch` post-PR). During the build-check gate, only tests related to changed files should run.

## Strategy

- Use `git diff` to detect files changed on the feature branch
- Map changed source files to their corresponding test files/packages using per-language heuristics
- Run only those targeted tests in the build-check step
- Fall back to the full test suite if mapping fails or no test files are found
- Update `test_hints` for all repos to encourage targeted testing during development

---

## File: `lib/config.py`

### Change 1: Add `targeted_test_command` field to `RepoInfo`

Add a new field after `test_command`:

```python
targeted_test_command: str = ""
"""Shell template for running only tests affected by the current
changes.  Must contain ``{targets}`` which will be replaced with
the language-specific list of test files / packages / modules
identified from ``git diff``.  When empty, the build-check step
falls back to *test_command*."""
```

### Change 2: Update all repo definitions

#### any-llm (Python)

```python
RepoInfo(
    name="any-llm",
    ...
    test_hints=(
        "Run ONLY the tests related to your changes: "
        "uv run pytest tests/unit/<relevant_test_file> -x -q. "
        "Do NOT run the full test suite during development -- it is slow "
        "and the full suite runs automatically in CI. "
        "Do NOT run integration tests. "
        "For linting use: uv run ruff check . && uv run mypy."
    ),
    test_command="uv run pytest tests/unit -x -q --timeout=60",
    targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
),
```

#### otari (Python)

```python
RepoInfo(
    name="otari",
    ...
    test_hints=(
        "Run ONLY the tests related to your changes: "
        "uv run pytest tests/<relevant_test_file> -x -q. "
        "Do NOT run the full test suite during development -- it is slow "
        "and the full suite runs automatically in CI. "
        "For linting: uv run ruff check . && uv run mypy."
    ),
    test_command="uv run pytest -x -q --timeout=60",
    targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
),
```

#### otari-sdk-rust (Rust)

```python
RepoInfo(
    name="otari-sdk-rust",
    ...
    test_hints=(
        "Run ONLY the tests related to your changes: "
        "cargo test <test_name_or_module> --all-features. "
        "Do NOT run the full test suite during development -- it is slow "
        "and the full suite runs automatically in CI. "
        "Lint: cargo clippy --all-features -- -D warnings && cargo fmt --check."
    ),
    test_command="cargo test --all-features",
    targeted_test_command="cargo test {targets} --all-features",
),
```

#### otari-sdk-go (Go)

```python
RepoInfo(
    name="otari-sdk-go",
    ...
    test_hints=(
        "Run ONLY the tests in packages you changed: "
        "go test ./path/to/package -race -count=1. "
        "Do NOT run the full test suite during development -- it is slow "
        "and the full suite runs automatically in CI. "
        "Lint: golangci-lint run."
    ),
    test_command="go test ./... -race -count=1",
    targeted_test_command="go test {targets} -race -count=1",
),
```

#### otari-sdk-ts (TypeScript)

```python
RepoInfo(
    name="otari-sdk-ts",
    ...
    test_hints=(
        "Run ONLY the tests related to your changes. Check package.json "
        "for the test runner (jest/vitest) and pass the relevant test "
        "file paths. Do NOT run the full test suite during development -- "
        "it is slow and the full suite runs automatically in CI. "
        "Lint: npx biome check . or the lint script in package.json."
    ),
    test_command="npm test",
    targeted_test_command="npx vitest run {targets}",
),
```

#### otari-ai (Python)

```python
RepoInfo(
    name="otari-ai",
    ...
    test_hints=(
        "Run ONLY the tests related to your changes: "
        "uv run pytest tests/<relevant_test_file> -x -q. "
        "Do NOT run the full test suite during development -- it is slow "
        "and the full suite runs automatically in CI. "
        "For linting: uv run ruff check . && uv run mypy."
    ),
    test_command="uv run pytest -x -q --timeout=60",
    targeted_test_command="uv run pytest {targets} -x -q --timeout=60",
),
```

---

## File: `lib/repo_runner.py`

### Change 3: Add `_get_changed_files()` helper

Insert after the existing imports / constants section (around line 50), before step_engineer:

```python
# ── Changed-file detection (for targeted test execution) ─────────────


def _get_changed_files(wt_path: Path, default_branch: str) -> list[str]:
    """Return files changed on the feature branch relative to the base.

    Uses ``git diff --name-only origin/<default_branch>...HEAD`` to find
    all files that differ between the upstream base and the current work.
    Returns an empty list on any git error (caller should fall back to
    the full test suite).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{default_branch}...HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().splitlines() if f]
    except (subprocess.TimeoutExpired, OSError):
        return []
```

### Change 4: Add per-language test target mapping functions

Insert right after `_get_changed_files`:

```python
def _map_python_test_targets(
    changed_files: list[str],
    wt_path: Path,
) -> list[str]:
    """Map changed Python source files to existing test files.

    Heuristic:
    - For a source file ``src/pkg/module.py``, look for
      ``tests/**/test_module.py``.
    - For a file already under ``tests/``, include it directly.
    - Only returns paths that actually exist in the worktree.
    """
    targets: list[str] = []
    seen: set[str] = set()

    for fpath in changed_files:
        if not fpath.endswith(".py"):
            continue

        # If the changed file is itself a test file, include it directly.
        if "/tests/" in fpath or fpath.startswith("tests/"):
            if (wt_path / fpath).exists() and fpath not in seen:
                targets.append(fpath)
                seen.add(fpath)
            continue

        # Extract the module base name and search for a matching test file.
        stem = Path(fpath).stem  # e.g. "models" from "src/app/models.py"
        test_name = f"test_{stem}.py"

        # Search the worktree for matching test files.
        for candidate in wt_path.rglob(test_name):
            rel = str(candidate.relative_to(wt_path))
            if rel not in seen:
                targets.append(rel)
                seen.add(rel)

    return targets


def _map_rust_test_targets(changed_files: list[str]) -> list[str]:
    """Map changed Rust files to ``cargo test`` filter arguments.

    Cargo accepts test name filters (substring match against test
    function names and module paths).  We extract module names from
    changed ``.rs`` source files and use them as filters.

    Returns a list of module-name filters.  If empty, caller should
    fall back to the full suite.
    """
    modules: list[str] = []
    seen: set[str] = set()
    for fpath in changed_files:
        if not fpath.endswith(".rs"):
            continue
        stem = Path(fpath).stem  # e.g. "client" from "src/client.rs"
        if stem in ("mod", "lib", "main"):
            continue  # too broad, skip
        if stem not in seen:
            modules.append(stem)
            seen.add(stem)
    return modules


def _map_go_test_targets(changed_files: list[str]) -> list[str]:
    """Map changed Go files to package paths for ``go test``.

    Go tests are package-scoped, so we collect the unique directories
    of changed ``.go`` files and format them as ``./dir/...`` targets.
    """
    packages: set[str] = set()
    for fpath in changed_files:
        if not fpath.endswith(".go"):
            continue
        pkg_dir = str(Path(fpath).parent)
        if pkg_dir == ".":
            packages.add("./...")
        else:
            packages.add(f"./{pkg_dir}/...")
    return sorted(packages)


def _map_ts_test_targets(
    changed_files: list[str],
    wt_path: Path,
) -> list[str]:
    """Map changed TypeScript files to test file paths.

    Heuristic:
    - For a source file ``src/foo.ts``, look for
      ``**/*.test.ts`` or ``**/*.spec.ts`` with matching stem.
    - For a file already matching test/spec pattern, include directly.
    """
    targets: list[str] = []
    seen: set[str] = set()
    ts_exts = (".ts", ".tsx", ".js", ".jsx")

    for fpath in changed_files:
        if not any(fpath.endswith(ext) for ext in ts_exts):
            continue

        # Already a test file?
        if ".test." in fpath or ".spec." in fpath:
            if (wt_path / fpath).exists() and fpath not in seen:
                targets.append(fpath)
                seen.add(fpath)
            continue

        # Search for matching test files.
        stem = Path(fpath).stem
        for pattern in (f"{stem}.test.*", f"{stem}.spec.*"):
            for candidate in wt_path.rglob(pattern):
                # Skip node_modules.
                rel = str(candidate.relative_to(wt_path))
                if "node_modules" in rel:
                    continue
                if rel not in seen:
                    targets.append(rel)
                    seen.add(rel)

    return targets


def _build_targeted_command(
    repo_info: RepoInfo,
    changed_files: list[str],
    wt_path: Path,
) -> str | None:
    """Build a targeted test command from changed files, or None to fall back.

    Returns the shell command string with ``{targets}`` replaced, or
    *None* if no targeted command is configured or no test targets
    could be identified (meaning the caller should run the full suite).
    """
    if not repo_info.targeted_test_command:
        return None

    lang = repo_info.language
    if lang == "python":
        targets = _map_python_test_targets(changed_files, wt_path)
        if not targets:
            return None
        return repo_info.targeted_test_command.format(targets=" ".join(targets))

    if lang == "rust":
        targets = _map_rust_test_targets(changed_files)
        if not targets:
            return None
        # cargo test accepts multiple filter args separated by spaces,
        # but they're OR'd only when passed as a single filter pattern.
        # For multiple modules, chain with '|' as a regex filter.
        filter_pattern = "|".join(targets)
        return repo_info.targeted_test_command.format(targets=filter_pattern)

    if lang == "go":
        targets = _map_go_test_targets(changed_files)
        if not targets:
            return None
        return repo_info.targeted_test_command.format(targets=" ".join(targets))

    if lang == "typescript":
        targets = _map_ts_test_targets(changed_files, wt_path)
        if not targets:
            return None
        return repo_info.targeted_test_command.format(targets=" ".join(targets))

    # Unknown language -- no targeted command.
    return None
```

### Change 5: Modify `step_build_check()` to use targeted tests

Replace the existing `step_build_check` function (lines 290-350) with:

```python
_BUILD_CHECK_TIMEOUT = 300  # 5 minutes max for test suite


def step_build_check(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
) -> bool:
    """Run relevant tests and return True if they pass.

    Tries to run only the tests affected by the current changes
    (via ``targeted_test_command`` + git-diff detection).  Falls back
    to the full ``test_command`` when targeted execution is not
    available or no impacted test files can be identified.

    The full test suite is left to CI (``step_ci_watch``).
    """
    repo_info = REPO_BY_NAME.get(repo_name)
    if not repo_info or not repo_info.test_command:
        return True

    wt_path = paths.worktree_path(slug, repo_name)
    log_file = paths.logs_dir(slug) / f"{repo_name}-build-check.log"

    # ── Attempt targeted test execution ──────────────────────────
    command: str | None = None
    changed_files = _get_changed_files(wt_path, repo_info.default_branch)

    if changed_files:
        command = _build_targeted_command(repo_info, changed_files, wt_path)

    if command:
        print(f"  [{repo_name}] Running targeted build check: {command}")
    else:
        command = repo_info.test_command
        print(f"  [{repo_name}] Running full build check (fallback): {command}")

    # ── Execute ──────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=_BUILD_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"  [{repo_name}] Build check timed out after {_BUILD_CHECK_TIMEOUT}s")
        log_file.write_text(
            f"Build check timed out after {_BUILD_CHECK_TIMEOUT}s\n",
            encoding="utf-8",
        )
        return False

    # Write output to log for debugging.
    output = result.stdout + "\n" + result.stderr
    log_file.write_text(output, encoding="utf-8")

    if result.returncode == 0:
        print(f"  [{repo_name}] Build check passed.")
        return True

    # Truncate output for the failure summary.
    lines = output.strip().splitlines()
    tail = "\n".join(lines[-30:]) if len(lines) > 30 else "\n".join(lines)
    print(f"  [{repo_name}] Build check FAILED (exit {result.returncode}):")
    print(f"    ...last lines:\n{tail}")

    # Write a failure summary for the engineer to use on retry.
    failure_file = paths.spec_file(slug, f"{repo_name}-build-failures.md")
    failure_file.write_text(
        f"# Build Check Failures: {repo_name}\n\n"
        f"**Command:** `{command}`\n"
        f"**Exit code:** {result.returncode}\n\n"
        f"## Output (last 80 lines)\n\n"
        f"```\n{chr(10).join(lines[-80:])}\n```\n",
        encoding="utf-8",
    )
    return False
```

### Change 6: Add `RepoInfo` to imports in repo_runner.py

At the top of `lib/repo_runner.py`, the import from `lib.config` needs to include `RepoInfo`:

```python
from lib.config import (
    CAVEMAN_PROMPT,
    CI_POLL_INTERVAL,
    MAX_CI_FIX_ROUNDS,
    MAX_REVIEW_ROUNDS,
    REPO_BY_NAME,
    RepoInfo,
    ProjectPaths,
    get_project_paths,
)
```

---

## File: `.opencode/agents/architect.md`

### Change 7: Update Testing Requirements guidance

In the per-repo spec template, update the `## Testing Requirements` section to explicitly mention targeted testing:

Replace:
```
## Testing Requirements
What tests to write. Include both unit and integration test expectations.
```

With:
```
## Testing Requirements
What tests to write. Include both unit and integration test expectations.

**Important:** Specify which test files/directories map to the changed source files so
engineers can run targeted tests during development.  The full test suite runs in CI;
during development, only impacted tests should be executed.
```

---

## Summary of all changes

| File | What changes |
|------|-------------|
| `lib/config.py` | New `targeted_test_command` field on `RepoInfo`; all 6 repos get `targeted_test_command` values; `test_hints` updated for all 6 repos to explicitly say "do NOT run full suite, CI handles that" |
| `lib/repo_runner.py` | New `_get_changed_files()`, `_map_python_test_targets()`, `_map_rust_test_targets()`, `_map_go_test_targets()`, `_map_ts_test_targets()`, `_build_targeted_command()` functions; `step_build_check()` rewritten to attempt targeted tests first, full-suite fallback; `RepoInfo` added to imports |
| `.opencode/agents/architect.md` | Testing Requirements template updated to ask architects to specify test file mappings |

## Fallback behavior

- If `git diff` fails or returns empty -> full suite (current behavior)
- If no test files can be mapped from changed files -> full suite
- If `targeted_test_command` is empty for a repo -> full suite
- If the targeted command times out or fails -> same error handling as before (failure summary written, engineer gets one retry)

## What is NOT changed

- `test_command` (full suite) remains for fallback and is still the canonical CI command
- `step_ci_watch` is unchanged -- it already runs the full CI suite post-PR
- `lib/workspace.py` injection logic is unchanged -- it already passes through `test_hints`
- The code-reviewer agent is unchanged
- The engineer agent prompt structure is unchanged (only `test_hints` text changes)
- The PR-creator agent is unchanged
