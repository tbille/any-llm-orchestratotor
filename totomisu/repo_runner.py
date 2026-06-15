"""Per-repo build pipeline: engineer -> review -> PR -> CI.

Runs inside a single tmux pane. Each repo flows independently.

Usage (called by the orchestrator via the hidden CLI subcommand):
    totomisu _repo-runner <slug> <repo-name>
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

from totomisu.config import (
    CAVEMAN_PROMPT,
    CI_POLL_INTERVAL,
    MAX_CI_FIX_ROUNDS,
    MAX_REVIEW_ROUNDS,
    PRAGMA_ENABLED,
    PRAGMA_VALIDATE_TIMEOUT,
    REPO_BY_NAME,
    RepoInfo,
    ProjectPaths,
    get_project_paths,
)
from totomisu.parse import (
    PragmaReport,
    ReviewVerdict,
    parse_pragma_report,
    parse_review_verdict,
)
from totomisu.status import update_repo_step


# ── Addressed-comments manifest ──────────────────────────────────────
#
# Tracks which PR review comments have already been sent to an engineer
# agent for fixing.  Prevents re-addressing the same comments on retry
# (e.g. after a crash or manual ``--fix-pr`` re-invocation).
#
# Manifest lives at  specs/<slug>/<repo>-addressed-comments.json
# Schema:
#   { "fix_rounds": [ { "timestamp", "commit_before", "commit_after",
#                        "comment_ids": [str, ...] } ] }


def _addressed_manifest_path(slug: str, repo_name: str, paths: ProjectPaths) -> Path:
    return paths.spec_file(slug, f"{repo_name}-addressed-comments.json")


def _load_addressed_manifest(slug: str, repo_name: str, paths: ProjectPaths) -> dict:
    """Load the addressed-comments manifest, or return an empty one."""
    import json as _json

    manifest_path = _addressed_manifest_path(slug, repo_name, paths)
    if not manifest_path.exists():
        return {"fix_rounds": []}
    try:
        return _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError):
        return {"fix_rounds": []}


def _save_addressed_manifest(
    slug: str, repo_name: str, paths: ProjectPaths, data: dict
) -> None:
    """Write the addressed-comments manifest."""
    import json as _json

    paths.ensure_spec_dirs(slug)
    manifest_path = _addressed_manifest_path(slug, repo_name, paths)
    manifest_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")


def _all_addressed_ids(manifest: dict) -> set[str]:
    """Return the union of all comment IDs across every fix round."""
    ids: set[str] = set()
    for fix_round in manifest.get("fix_rounds", []):
        ids.update(fix_round.get("comment_ids", []))
    return ids


def _get_head_sha(wt_path: Path) -> str:
    """Return the current HEAD commit SHA for a worktree."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# ── Reply to & resolve PR comments ───────────────────────────────────


def _reply_and_resolve_comments(
    wt_path: Path,
    pr_number: int,
    new_inline: list[dict],
    new_general: list[dict],
    commit_sha: str,
    repo_name: str,
) -> None:
    """Reply to each addressed comment and resolve its review thread.

    For inline review comments:
      1. Reply via REST: POST /repos/{owner}/{repo}/pulls/{number}/comments/{id}/replies
      2. Resolve the thread via GraphQL ``resolveReviewThread`` mutation.

    For general conversation comments:
      Reply via ``gh pr comment`` (no thread to resolve).

    Failures are logged but never abort the pipeline — this is best-effort.
    """
    import json as _json

    short_sha = commit_sha[:10]
    reply_body = f"Addressed in {short_sha}."

    # ── Reply to & resolve inline review comments ────────────────────
    # First, build a mapping from comment node_id -> thread node_id so
    # we can resolve the right threads.
    thread_map: dict[str, str] = {}  # comment_node_id -> thread_node_id
    if new_inline:
        thread_map = _fetch_thread_ids_for_comments(wt_path, pr_number, repo_name)

    for ic in new_inline:
        ic_id = ic.get("id")
        ic_node_id = ic.get("node_id", "")
        if not ic_id:
            continue

        # Reply to the comment.
        reply_result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments/{ic_id}/replies",
                "-f",
                f"body={reply_body}",
            ],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if reply_result.returncode != 0:
            print(
                f"  [{repo_name}] Failed to reply to inline comment {ic_id}: "
                f"{reply_result.stderr.strip()[:120]}",
                file=sys.stderr,
            )

        # Resolve the thread if we have its ID.
        thread_id = thread_map.get(ic_node_id, "")
        if thread_id:
            _resolve_thread(wt_path, thread_id, repo_name)

    # ── Reply to general conversation comments ───────────────────────
    for comment in new_general:
        comment_node_id = comment.get("id", "")
        if not comment_node_id:
            continue
        author = comment.get("author", {}).get("login", "unknown")
        original_body = comment.get("body", "").strip()
        # Include a short snippet of the original comment for context.
        snippet = original_body[:80]
        if len(original_body) > 80:
            snippet += "…"
        general_reply = f"> @{author}: {snippet}\n\n{reply_body}"
        _reply_to_issue_comment(wt_path, general_reply, repo_name)

    commented = len(new_inline) + len(new_general)
    if commented:
        print(f"  [{repo_name}] Replied to {commented} comment(s) on PR.")


def _fetch_thread_ids_for_comments(
    wt_path: Path,
    pr_number: int,
    repo_name: str,
) -> dict[str, str]:
    """Fetch a mapping of comment node_id -> thread node_id via GraphQL.

    Uses pagination to handle PRs with many review threads.
    Returns an empty dict on any error.
    """
    import json as _json

    # Query review threads on the PR and their first few comments to
    # map comment node IDs to thread node IDs.
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              comments(first: 100) {
                nodes { id }
              }
            }
          }
        }
      }
    }
    """

    # Resolve owner/repo from the git remote.
    nwo = _get_nwo(wt_path)
    if not nwo:
        return {}
    owner, repo = nwo

    mapping: dict[str, str] = {}
    cursor: str | None = None

    for _ in range(20):  # safety cap on pagination
        gql_args = [
            "gh",
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={pr_number}",
            "-f",
            f"query={query}",
        ]
        if cursor:
            gql_args += ["-F", f"cursor={cursor}"]
        else:
            gql_args += ["-F", "cursor=null"]

        result = subprocess.run(
            gql_args, cwd=str(wt_path), capture_output=True, text=True
        )
        if result.returncode != 0:
            print(
                f"  [{repo_name}] Failed to fetch review threads: "
                f"{result.stderr.strip()[:120]}",
                file=sys.stderr,
            )
            break

        try:
            gql_data = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            break

        threads_data = (
            gql_data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        for thread in threads_data.get("nodes", []):
            thread_id = thread.get("id", "")
            for comment_node in thread.get("comments", {}).get("nodes", []):
                comment_id = comment_node.get("id", "")
                if comment_id and thread_id:
                    mapping[comment_id] = thread_id

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
        else:
            break

    return mapping


def _resolve_thread(wt_path: Path, thread_id: str, repo_name: str) -> None:
    """Resolve a single review thread via GraphQL mutation."""
    import json as _json

    mutation = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread { id isResolved }
      }
    }
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-F",
            f"threadId={thread_id}",
            "-f",
            f"query={mutation}",
        ],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  [{repo_name}] Failed to resolve thread {thread_id}: "
            f"{result.stderr.strip()[:120]}",
            file=sys.stderr,
        )


def _reply_to_issue_comment(
    wt_path: Path,
    body: str,
    repo_name: str,
) -> None:
    """Post a top-level comment on the PR referencing addressed feedback.

    General PR comments are issue comments — they don't support threaded
    replies.  We post a new comment that quotes the original for context.
    ``gh pr comment`` discovers the PR automatically from the worktree.
    """
    result = subprocess.run(
        ["gh", "pr", "comment", "--body", body],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  [{repo_name}] Failed to reply to general comment: "
            f"{result.stderr.strip()[:120]}",
            file=sys.stderr,
        )


def _get_nwo(wt_path: Path) -> tuple[str, str] | None:
    """Return (owner, repo) from the git remote of *wt_path*."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "owner,name", "--jq", ".owner.login,.name"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().splitlines()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


# ── Opencode runner ───────────────────────────────────────────────────


def _run_opencode(
    wt_path: Path,
    prompt_file: Path,
    file_args: list[str],
    log_file: Path,
) -> int:
    """Run opencode and tee output to a log file. Returns exit code."""
    parts = [
        "opencode",
        "run",
        "--dir",
        shlex.quote(str(wt_path)),
        "--dangerously-skip-permissions",
    ]
    parts += file_args
    parts += ["-f", shlex.quote(str(prompt_file))]
    parts += ["--", shlex.quote("Follow the instructions in the attached prompt file.")]

    cmd = " ".join(parts) + f" 2>&1 | tee -a {shlex.quote(str(log_file))}"
    result = subprocess.run(["sh", "-c", cmd], cwd=str(wt_path))
    if result.returncode != 0:
        print(
            f"  [WARN] opencode exited with code {result.returncode}",
            file=sys.stderr,
        )
    return result.returncode


# Phases where detailed, clear output matters more than token savings.
_VERBOSE_PHASES = frozenset({"review", "cross-review", "pr"})

# Scoping instruction appended to every per-repo agent prompt so the agent
# stays within the repository it was launched in and doesn't wander into
# sibling repos or parent directories.
_REPO_SCOPE_INSTRUCTION = (
    " IMPORTANT: You are working ONLY on the {repo_name} repository. "
    "Do NOT access, read, or reference any other repositories or parent "
    "directories. All the context you need is in the attached files and "
    "the code in this directory."
)


def _write_prompt(path: Path, message: str, *, phase: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if phase in _VERBOSE_PHASES else CAVEMAN_PROMPT
    path.write_text(prefix + message, encoding="utf-8")


# ── Step: Engineer ────────────────────────────────────────────────────


def step_engineer(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
    *,
    is_fix_round: bool = False,
) -> None:
    """Run the engineer agent."""
    wt_path = paths.worktree_path(slug, repo_name)
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    log_file = paths.logs_dir(slug) / f"{repo_name}-engineer.log"
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"

    file_args: list[str] = []
    if spec_file.exists():
        file_args += ["-f", str(spec_file)]

    if is_fix_round:
        review_file = paths.spec_file(slug, f"{repo_name}-review.md")
        if review_file.exists():
            file_args += ["-f", str(review_file)]
        build_failure_file = paths.spec_file(slug, f"{repo_name}-build-failures.md")
        if build_failure_file.exists():
            file_args += ["-f", str(build_failure_file)]
        pragma_violations_file = paths.spec_file(
            slug, f"{repo_name}-pragma-violations.md"
        )
        if pragma_violations_file.exists():
            file_args += ["-f", str(pragma_violations_file)]

    scope_note = ""
    if repo_info and repo_info.scope_notes:
        scope_note = f" SCOPE NOTE: {repo_info.scope_notes}"

    test_note = ""
    if repo_info and repo_info.test_hints:
        test_note = f" TESTING: {repo_info.test_hints}"

    commit_instructions = (
        " As you implement, commit your work in small, atomic commits. "
        "Each commit should be a single logical change (one concept per "
        "commit) with a clear, descriptive message in imperative mood. "
        "Good examples: 'Add BatchRequest and BatchResponse types', "
        "'Implement batch endpoint handler', 'Add unit tests for batch "
        "processing'. Bad: one giant 'implement feature' commit."
        f"{test_note}"
    )

    repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)

    if is_fix_round:
        message = (
            f"Fix the issues found in the attached file(s). This is a {lang} project. "
            f"If a review file is attached, address the review feedback. "
            f"If a build-failures file is attached, fix the failing tests. "
            f"If a pragma-violations file is attached, fix every HARD "
            f"violation it lists -- those block the pipeline. "
            f"NEVER run the full test suite. Only run the specific test files "
            f"related to the files you changed. The full suite runs in CI."
            f"{scope_note}{commit_instructions}{repo_scope}"
        )
    else:
        pragma_note = ""
        if repo_info and repo_info.pragma_validators:
            pragma_note = (
                " ENFORCEMENT: This repo is guarded by agent-pragma. "
                "After you finish, `/validate` will run and any HARD "
                "violation blocks the pipeline. Follow idiomatic "
                f"{lang} patterns: proper error handling, no secrets in "
                "code, doc-comments on exported APIs, no swallowed "
                "exceptions."
            )
        message = (
            f"Implement the feature described in the attached spec for this "
            f"{lang} project. Follow the repository's existing patterns and "
            f"conventions. Write tests for your changes. "
            f"NEVER run the full test suite. Only run the specific test files "
            f"related to the files you changed. The full suite runs in CI."
            f"{scope_note}{commit_instructions}{pragma_note}{repo_scope}"
        )

    # Simple-bug path: no spec file, use input and investigation note.
    if not spec_file.exists():
        input_file = paths.spec_file(slug, "input.md")
        if input_file.exists():
            file_args += ["-f", str(input_file)]
        investigation_file = paths.spec_file(slug, f"{repo_name}-investigation.md")
        if investigation_file.exists():
            file_args += ["-f", str(investigation_file)]
        message = (
            f"Fix the bug described in the attached issue for this {lang} project. "
            f"Follow the repository's existing patterns. "
            f"NEVER run the full test suite. Only run the specific test files "
            f"related to the files you changed. The full suite runs in CI."
            f"{scope_note}{commit_instructions}{repo_scope}"
        )

    prompt_file = paths.logs_dir(slug) / f"{repo_name}-engineer-prompt.md"
    _write_prompt(prompt_file, message, phase="engineer")
    rc = _run_opencode(wt_path, prompt_file, file_args, log_file)
    if rc != 0:
        print(f"  [{repo_name}] Engineer agent exited with code {rc}", file=sys.stderr)


# ── Step: Investigate (simple bugs only) ──────────────────────────────


_INVESTIGATION_TIMEOUT = 120  # 2 minutes for a quick investigation


def step_investigate(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
) -> None:
    """Run a quick headless investigation for simple bugs.

    Reads the issue and scans the repo to produce a one-paragraph
    investigation note identifying the likely root cause and which
    files to change.  Gives the engineer agent focused context rather
    than a raw issue body.

    Only called for simple bugs where no per-repo spec exists.
    """
    wt_path = paths.worktree_path(slug, repo_name)
    input_file = paths.spec_file(slug, "input.md")
    investigation_file = paths.spec_file(slug, f"{repo_name}-investigation.md")
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"

    if investigation_file.exists():
        print(f"  [{repo_name}] Investigation note already exists, skipping.")
        return

    if not input_file.exists():
        return

    message = (
        f"You are investigating a bug in this {lang} repository. "
        f"Read the attached issue and scan the codebase to identify: "
        f"1. The likely root cause (which file(s) and function(s)). "
        f"2. A brief fix approach (1-2 sentences). "
        f"3. Which tests to update or add. "
        f"Write a SHORT investigation note (under 500 words) to: "
        f"{investigation_file} "
        f"Do NOT make any code changes. Only investigate and write the note."
    )

    log_file = paths.logs_dir(slug) / f"{repo_name}-investigate.log"
    prompt_file = paths.logs_dir(slug) / f"{repo_name}-investigate-prompt.md"
    _write_prompt(prompt_file, message, phase="investigate")

    file_args = ["-f", str(input_file)]

    print(f"  [{repo_name}] Running quick investigation...")
    _run_opencode(wt_path, prompt_file, file_args, log_file)


# ── Targeted test detection ──────────────────────────────────────────
#
# These helpers detect which files changed on the feature branch and
# map them to test targets so the build-check step can run only the
# impacted tests.  The full suite is left to CI (step_ci_watch).


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
        # cargo test accepts a filter pattern; multiple modules are
        # OR'd via pipe as a regex.
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


# ── Step: Build check (pre-review test execution) ────────────────────


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


# ── Step: agent-pragma /validate ──────────────────────────────────────


def _pragma_installed(paths: ProjectPaths) -> bool:
    """Return True when agent-pragma appears to be installed in the workspace.

    Checks for ``.agent-pragma/`` (the pinned checkout) and at least one
    command file under ``.opencode/commands/``.  Missing either means
    ``totomisu init`` did not complete the pragma step (e.g. ``make``
    was absent) -- we skip validation silently rather than failing.
    """
    pragma_checkout = paths.pragma_dir
    commands_dir = paths.root / ".opencode" / "commands"
    if not pragma_checkout.exists():
        return False
    if not commands_dir.exists() or not any(commands_dir.glob("*.md")):
        return False
    return True


def step_pragma_validate(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
) -> bool:
    """Run agent-pragma's ``/validate`` command and record HARD violations.

    Returns True when validation passes (or is skipped); False when HARD
    violations were found and the engineer should run a fix round.

    Non-fatal on any internal error: a broken pragma install must never
    block the pipeline.  In that case we log and return True so the
    existing review step continues normally.
    """
    if not PRAGMA_ENABLED:
        return True

    repo_info = REPO_BY_NAME.get(repo_name)
    if not repo_info or not repo_info.pragma_validators:
        return True

    if not _pragma_installed(paths):
        print(
            f"  [{repo_name}] agent-pragma not installed in workspace; "
            f"skipping /validate."
        )
        return True

    wt_path = paths.worktree_path(slug, repo_name)
    log_file = paths.logs_dir(slug) / f"{repo_name}-pragma.log"
    report_file = paths.spec_file(slug, f"{repo_name}-pragma-report.md")

    validators_hint = ", ".join(repo_info.pragma_validators)
    print(f"  [{repo_name}] Running /validate ({validators_hint})...")

    # opencode run --dir <workspace> executes the /validate command.
    # We pass a one-line prompt asking opencode to run the command and
    # print its markdown report verbatim so the parser can extract the
    # table + verdict.
    cmd = [
        "opencode",
        "run",
        "--dir",
        str(wt_path),
        "--dangerously-skip-permissions",
        "--",
        "Run the /validate slash command and return its full markdown "
        "report verbatim. Do not add commentary.",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PRAGMA_VALIDATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(
            f"  [{repo_name}] /validate timed out after "
            f"{PRAGMA_VALIDATE_TIMEOUT}s -- skipping (non-blocking).",
            file=sys.stderr,
        )
        log_file.write_text("TIMEOUT\n", encoding="utf-8")
        return True

    combined = result.stdout + "\n" + result.stderr
    log_file.write_text(combined, encoding="utf-8")

    if result.returncode != 0:
        print(
            f"  [{repo_name}] /validate exit={result.returncode}; "
            f"treating as non-blocking."
        )
        return True

    report = parse_pragma_report(combined)
    _write_pragma_report_file(report_file, repo_name, repo_info, report)

    print(
        f"  [{repo_name}] pragma: verdict={report.verdict} "
        f"HARD={report.hard} SHOULD={report.should} WARN={report.warn}"
    )

    if not report.blocked:
        return True

    # Write the engineer-facing violations file (fed into the fix round).
    violations_file = paths.spec_file(slug, f"{repo_name}-pragma-violations.md")
    violations_file.write_text(
        _format_violations_for_engineer(repo_name, report),
        encoding="utf-8",
    )
    return False


def _write_pragma_report_file(
    report_file: Path,
    repo_name: str,
    repo_info: RepoInfo,
    report: PragmaReport,
) -> None:
    """Persist the parsed pragma report for debugging and resumability."""
    lines = [
        f"# agent-pragma report: {repo_name}",
        "",
        f"**Validators:** {', '.join(repo_info.pragma_validators)}",
        f"**Verdict:** {report.verdict}",
        f"**HARD:** {report.hard}  **SHOULD:** {report.should}  "
        f"**WARN:** {report.warn}",
        "",
    ]
    if report.violations_md:
        lines.append(report.violations_md)
        lines.append("")
    report_file.write_text("\n".join(lines), encoding="utf-8")


def _format_violations_for_engineer(
    repo_name: str,
    report: PragmaReport,
) -> str:
    """Format HARD violations as actionable engineer context."""
    return (
        f"# Pragma HARD Violations: {repo_name}\n\n"
        f"The `/validate` command found {report.hard} HARD violation(s) "
        f"that MUST be fixed before this change can proceed to review.\n\n"
        f"{report.violations_md or '(no violation details captured)'}\n\n"
        f"Fix each HARD violation above.  After fixing, the pipeline "
        f"will re-run `/validate` automatically.  Do not silence "
        f"validators -- address the underlying issue.\n"
    )


# ── Step: Review ──────────────────────────────────────────────────────


def step_review(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
    *,
    is_followup: bool = False,
) -> ReviewVerdict:
    """Run the code review agent. Returns a structured ReviewVerdict.

    When *is_followup* is True, the reviewer focuses on verifying that
    previously flagged issues were fixed rather than doing a full review
    from scratch.  The prior review file is attached as context.
    """
    wt_path = paths.worktree_path(slug, repo_name)
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    review_file = paths.spec_file(slug, f"{repo_name}-review.md")
    log_file = paths.logs_dir(slug) / f"{repo_name}-review.log"
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"

    file_args: list[str] = []
    if spec_file.exists():
        file_args += ["-f", str(spec_file)]

    repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)

    verdict_instruction = (
        " IMPORTANT: At the very end of the review file, include a machine-readable "
        "verdict as an HTML comment on its own line: "
        '<!-- VERDICT: {"status": "PASS", "blockers": 0, "majors": 0, "minors": 0} -->'
    )

    pragma_hint = ""
    if repo_info and repo_info.pragma_validators and _pragma_installed(paths):
        pragma_hint = (
            " Start by running the `/review` slash command to get the "
            "deterministic validator findings, then incorporate its output "
            "into your write-up. Treat every pragma HARD violation as a "
            "review BLOCKER."
        )

    if is_followup and review_file.exists():
        # Attach the prior review so the reviewer knows what to verify.
        file_args += ["-f", str(review_file)]
        message = (
            f"This is a FOLLOW-UP review for this {lang} repository. "
            f"The attached review file contains the previous review with "
            f"BLOCKER and MAJOR issues that the engineer was asked to fix. "
            f"Focus on verifying that those specific issues were addressed. "
            f"Also check for any new regressions introduced by the fixes. "
            f"Do NOT re-review the entire codebase from scratch. "
            f"Write your updated review to: {review_file} "
            f"Use this format: "
            f"## Status: PASS or NEEDS_CHANGES "
            f"## Previously Flagged Issues (status of each: FIXED or STILL_OPEN) "
            f"## New Issues Found (if any) "
            f"## Recommendations (list improvements)"
            f"{pragma_hint}{verdict_instruction}{repo_scope}"
        )
    else:
        message = (
            f"Review the code changes in this {lang} repository. "
            f"Compare against the attached spec. Check for: "
            f"spec compliance, code quality and {lang}-idiomatic patterns, "
            f"test coverage, error handling, backwards compatibility. "
            f"Write your review to: {review_file} "
            f"Use this format: "
            f"## Status: PASS or NEEDS_CHANGES "
            f"## Issues Found (list each issue) "
            f"## Recommendations (list improvements)"
            f"{pragma_hint}{verdict_instruction}{repo_scope}"
        )

    prompt_file = paths.logs_dir(slug) / f"{repo_name}-review-prompt.md"
    _write_prompt(prompt_file, message, phase="review")
    rc = _run_opencode(wt_path, prompt_file, file_args, log_file)
    if rc != 0:
        print(f"  [{repo_name}] Review agent exited with code {rc}", file=sys.stderr)

    return parse_review_verdict(review_file)


# ── Step: PR ──────────────────────────────────────────────────────────


def _try_deterministic_pr(
    slug: str,
    repo_name: str,
    wt_path: Path,
    paths: ProjectPaths,
    *,
    draft: bool = False,
) -> str | None:
    """Attempt to create a PR using shell commands only (no AI agent).

    Steps:
    1. Commit any uncommitted changes.
    2. Push the branch.
    3. Generate a PR body from the spec and commit log.
    4. Create the PR via ``gh pr create``.

    Returns the PR URL on success, or ``None`` if the caller should
    fall back to the AI agent (e.g. the repo has a complex PR template
    or the deterministic path failed).
    """
    from totomisu.pr import _find_pr_template

    # If the repo has a PR template, fall back to AI to fill it properly.
    template = _find_pr_template(wt_path)
    if template:
        return None

    repo_info = REPO_BY_NAME.get(repo_name)
    base_branch = repo_info.default_branch if repo_info else "main"

    # 1. Commit any uncommitted changes.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(wt_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Final uncommitted changes for {slug}"],
            cwd=str(wt_path),
            capture_output=True,
        )

    # 2. Push the branch.
    push = subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        print(f"  [{repo_name}] git push failed: {push.stderr.strip()[:200]}")
        return None

    # 3. Generate PR title and body from commit log.
    log_result = subprocess.run(
        ["git", "log", f"origin/{base_branch}..HEAD", "--pretty=format:%s"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    commits = (
        log_result.stdout.strip().splitlines() if log_result.stdout.strip() else []
    )

    # Build a descriptive PR title from the spec's Context section.
    title = f"{slug}: {repo_name}"
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    if spec_file.exists():
        spec_text = spec_file.read_text(encoding="utf-8")
        for line in spec_text.splitlines():
            stripped = line.strip()
            # Use the first heading after "# Implementation Spec:" as base.
            if stripped.startswith("# ") and "Implementation Spec" not in stripped:
                title = f"[{slug}] {stripped.lstrip('# ').strip()}"
                break
        else:
            # Fall back to first sentence of the Context section.
            for i, line in enumerate(spec_text.splitlines()):
                if line.strip().startswith("## Context"):
                    rest = spec_text.splitlines()[i + 1 :]
                    for ctx_line in rest:
                        ctx_stripped = ctx_line.strip()
                        if ctx_stripped and not ctx_stripped.startswith("#"):
                            # First non-empty, non-heading line.
                            first_sentence = ctx_stripped.split(". ")[0]
                            if len(first_sentence) > 80:
                                first_sentence = first_sentence[:77] + "..."
                            title = f"[{slug}] {first_sentence}"
                            break
                    break

    # Build body from spec summary + commit list.
    body_lines = ["## Summary", ""]
    if spec_file.exists():
        # Use the first paragraph of the spec's Context section as summary.
        for line in spec_text.splitlines():
            if line.startswith("## Context"):
                idx = spec_text.index(line) + len(line)
                rest = spec_text[idx:].strip().split("\n\n")[0]
                body_lines.append(rest.strip())
                break
        else:
            body_lines.append(f"Implementation for {slug} in {repo_name}.")
    else:
        body_lines.append(f"Implementation for {slug} in {repo_name}.")

    body_lines += ["", "## Changes", ""]
    for commit in commits[:20]:
        body_lines.append(f"- {commit}")

    body_lines += ["", "## Testing", "", "- See commit history for test additions."]
    body = "\n".join(body_lines)

    # 4. Create the PR.
    gh_cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        gh_cmd.append("--draft")
    pr_result = subprocess.run(
        gh_cmd,
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if pr_result.returncode != 0:
        print(f"  [{repo_name}] gh pr create failed: {pr_result.stderr.strip()[:200]}")
        return None

    pr_url = pr_result.stdout.strip()
    print(f"  [{repo_name}] PR created: {pr_url}")
    return pr_url


def step_pr(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
    *,
    draft: bool = False,
) -> None:
    """Create a pull request.

    Tries a fast deterministic path first (shell commands only).
    Falls back to the AI agent if the repo has a PR template or if
    the deterministic path fails.
    """
    from totomisu.status import set_repo_pr_url

    wt_path = paths.worktree_path(slug, repo_name)

    # Rebase onto the latest base branch before pushing the PR.
    step_rebase_on_base(slug, repo_name, paths)

    # Fast path: no PR template, use shell commands.
    pr_url = _try_deterministic_pr(slug, repo_name, wt_path, paths, draft=draft)
    if pr_url:
        set_repo_pr_url(slug, repo_name, pr_url, paths)
        return

    # Slow path: fall back to AI agent (PR template or deterministic failure).
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    log_file = paths.logs_dir(slug) / f"{repo_name}-pr.log"
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"

    file_args: list[str] = []
    if spec_file.exists():
        file_args += ["-f", str(spec_file)]

    from totomisu.pr import _find_pr_template

    template = _find_pr_template(wt_path)
    template_instruction = ""
    if template:
        file_args += ["-f", str(template)]
        template_instruction = (
            f" IMPORTANT: This repository has a PR template at {template.name}. "
            f"It is attached. You MUST follow its structure when writing the PR body."
        )

    draft_instruction = ""
    if draft:
        draft_instruction = (
            " IMPORTANT: Create this PR as a DRAFT (use `gh pr create --draft`)."
            " The code review did not fully pass, so this PR needs human review."
        )

    repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)
    message = (
        f"Create a pull request for the changes in this {lang} repository. "
        f"Steps: "
        f"1. Review all uncommitted changes and commit them if needed. "
        f"2. Push the branch to the remote. "
        f"3. Create a pull request using `gh pr create`. "
        f"4. Write a clear title and description summarizing the changes. "
        f"5. Reference the original issue if applicable."
        f"{template_instruction}{draft_instruction}{repo_scope}"
    )

    prompt_file = paths.logs_dir(slug) / f"{repo_name}-pr-prompt.md"
    _write_prompt(prompt_file, message, phase="pr")
    rc = _run_opencode(wt_path, prompt_file, file_args, log_file)
    if rc != 0:
        print(f"  [{repo_name}] PR agent exited with code {rc}", file=sys.stderr)

    # Try to discover the PR URL the agent created and cache it.
    try:
        discover = subprocess.run(
            ["gh", "pr", "view", "--json", "url", "--jq", ".url"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=12,
        )
        if discover.returncode == 0 and discover.stdout.strip():
            set_repo_pr_url(slug, repo_name, discover.stdout.strip(), paths)
    except (subprocess.TimeoutExpired, Exception):
        pass


# ── Step: CI watch ────────────────────────────────────────────────────


def step_ci_watch(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
    *,
    max_fix_rounds: int = MAX_CI_FIX_ROUNDS,
    poll_interval: int = CI_POLL_INTERVAL,
) -> None:
    """Poll CI and fix failures."""
    from totomisu.pr import _get_ci_status, _collect_ci_failure_logs

    wt_path = paths.worktree_path(slug, repo_name)

    for fix_round in range(max_fix_rounds + 1):
        status = "pending"
        detail = ""
        while True:
            status, detail = _get_ci_status(wt_path)
            if status in ("pass", "fail", "no-ci", "no-pr"):
                break
            print(f"  [{repo_name}] CI pending: {detail}")
            time.sleep(poll_interval)

        if status == "pass":
            print(f"  [{repo_name}] CI passed: {detail}")
            return
        if status in ("no-ci", "no-pr"):
            print(f"  [{repo_name}] No CI: {detail}")
            return

        print(f"  [{repo_name}] CI failed: {detail}")

        if fix_round >= max_fix_rounds:
            print(f"  [{repo_name}] Max CI fix rounds reached.")
            return

        # Fix CI failures.
        failure_log = _collect_ci_failure_logs(wt_path)
        ci_log_file = paths.spec_file(slug, f"{repo_name}-ci-failures.md")
        ci_log_file.write_text(failure_log, encoding="utf-8")

        repo_info = REPO_BY_NAME.get(repo_name)
        lang = repo_info.language if repo_info else "unknown"
        log_file = paths.logs_dir(slug) / f"{repo_name}-ci-fix.log"

        file_args: list[str] = []
        if ci_log_file.exists():
            file_args += ["-f", str(ci_log_file)]

        test_note = ""
        if repo_info and repo_info.test_hints:
            test_note = f" TESTING: {repo_info.test_hints}"

        repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)
        message = (
            f"The CI pipeline is failing for this {lang} project. "
            f"The attached file lists the failed checks. "
            f"Investigate the failures by reading the error output carefully. "
            f"Fix the issues. Run only the specific failing tests to verify "
            f"your fix -- NEVER run the full test suite. "
            f"Commit your fixes and push."
            f"{test_note}{repo_scope}"
        )

        print(f"  [{repo_name}] Fixing CI (round {fix_round + 1})...")
        update_repo_step(slug, repo_name, f"ci-fix (round {fix_round + 1})", paths)

        prompt_file = paths.logs_dir(slug) / f"{repo_name}-ci-fix-prompt.md"
        _write_prompt(prompt_file, message, phase="ci-fix")
        rc = _run_opencode(wt_path, prompt_file, file_args, log_file)
        if rc != 0:
            print(
                f"  [{repo_name}] CI fix agent exited with code {rc}",
                file=sys.stderr,
            )

        # Rebase onto the latest base branch before pushing.
        step_rebase_on_base(slug, repo_name, paths)

        push_result = subprocess.run(
            ["git", "push"], cwd=str(wt_path), capture_output=True, text=True
        )
        if push_result.returncode != 0:
            print(
                f"  [{repo_name}] git push failed after CI fix: "
                f"{push_result.stderr.strip()[:200]}",
                file=sys.stderr,
            )


# ── Step: Rebase on base branch ───────────────────────────────────────

_MAX_REBASE_CONFLICT_ROUNDS = 5  # Safety cap for conflict resolution loops.


def step_rebase_on_base(
    slug: str,
    repo_name: str,
    paths: ProjectPaths,
) -> bool:
    """Fetch the base branch and rebase the feature branch onto it.

    If the rebase produces merge conflicts, an opencode agent is invoked
    to resolve them, after which the rebase is continued.  This repeats
    until the rebase finishes or the safety cap is reached.

    Returns ``True`` if the branch was successfully rebased (or was
    already up-to-date), ``False`` if the rebase had to be aborted.
    """
    wt_path = paths.worktree_path(slug, repo_name)
    repo_info = REPO_BY_NAME.get(repo_name)
    base_branch = repo_info.default_branch if repo_info else "main"
    lang = repo_info.language if repo_info else "unknown"
    log_file = paths.logs_dir(slug) / f"{repo_name}-rebase.log"

    # 1. Fetch the latest base branch from origin.
    print(f"  [{repo_name}] Fetching origin/{base_branch}...")
    fetch = subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        print(
            f"  [{repo_name}] git fetch failed: {fetch.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return False

    # 2. Check whether a rebase is needed at all.
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", f"origin/{base_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    remote_head = subprocess.run(
        ["git", "rev-parse", f"origin/{base_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if (
        merge_base.returncode == 0
        and remote_head.returncode == 0
        and merge_base.stdout.strip() == remote_head.stdout.strip()
    ):
        print(f"  [{repo_name}] Already up-to-date with origin/{base_branch}.")
        return True

    # 3. Attempt the rebase.
    print(f"  [{repo_name}] Rebasing onto origin/{base_branch}...")
    rebase = subprocess.run(
        ["git", "rebase", f"origin/{base_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )

    if rebase.returncode == 0:
        print(f"  [{repo_name}] Rebase completed cleanly.")
        _force_push_after_rebase(wt_path, repo_name)
        return True

    # 4. Conflict loop — let the agent resolve conflicts, then continue.
    for conflict_round in range(1, _MAX_REBASE_CONFLICT_ROUNDS + 1):
        print(
            f"  [{repo_name}] Rebase conflict (round {conflict_round}/"
            f"{_MAX_REBASE_CONFLICT_ROUNDS}) — invoking agent..."
        )

        # Collect conflict details.
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        conflicted_files = diff_result.stdout.strip()

        diff_full = subprocess.run(
            ["git", "diff"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )

        conflict_info = (
            f"# Rebase Conflict — {repo_name}\n\n"
            f"The rebase onto `origin/{base_branch}` produced merge conflicts.\n\n"
            f"## Conflicted files\n\n```\n{conflicted_files}\n```\n\n"
            f"## Full diff (with conflict markers)\n\n"
            f"```diff\n{diff_full.stdout[:30000]}\n```\n"
        )
        conflict_file = paths.spec_file(slug, f"{repo_name}-rebase-conflicts.md")
        conflict_file.write_text(conflict_info, encoding="utf-8")

        repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)
        message = (
            f"The git rebase of this {lang} project onto origin/{base_branch} "
            f"has produced merge conflicts.  The conflicted files and the full "
            f"diff with conflict markers are in the attached file.  "
            f"Resolve ALL conflicts in the working tree by editing the files "
            f"to their correct final state (remove all conflict markers).  "
            f"After editing, stage every resolved file with `git add`.  "
            f"Do NOT run `git rebase --continue` — I will do that.  "
            f"Do NOT commit."
            f"{repo_scope}"
        )

        prompt_file = (
            paths.logs_dir(slug) / f"{repo_name}-rebase-prompt-{conflict_round}.md"
        )
        _write_prompt(prompt_file, message, phase="rebase")
        _run_opencode(wt_path, prompt_file, ["-f", str(conflict_file)], log_file)

        # Continue the rebase after the agent has staged resolved files.
        cont = subprocess.run(
            ["git", "-c", "core.editor=true", "rebase", "--continue"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if cont.returncode == 0:
            print(
                f"  [{repo_name}] Rebase completed after {conflict_round} conflict round(s)."
            )
            _force_push_after_rebase(wt_path, repo_name)
            return True

        # If rebase --continue itself hits another conflicting commit,
        # loop again.
        print(
            f"  [{repo_name}] Rebase still has conflicts after round {conflict_round}."
        )

    # Safety cap reached — abort the rebase so the worktree is usable.
    print(
        f"  [{repo_name}] Aborting rebase after {_MAX_REBASE_CONFLICT_ROUNDS} "
        f"conflict rounds.",
        file=sys.stderr,
    )
    subprocess.run(
        ["git", "rebase", "--abort"],
        cwd=str(wt_path),
        capture_output=True,
    )
    return False


def _force_push_after_rebase(wt_path: Path, repo_name: str) -> None:
    """Force-push the rebased branch (with lease for safety)."""
    print(f"  [{repo_name}] Force-pushing rebased branch...")
    push = subprocess.run(
        ["git", "push", "--force-with-lease"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        print(
            f"  [{repo_name}] git push --force-with-lease failed: "
            f"{push.stderr.strip()[:200]}",
            file=sys.stderr,
        )
    else:
        print(f"  [{repo_name}] Rebased branch pushed.")


# ── Step: Fix PR feedback ─────────────────────────────────────────────


def step_fix_pr(slug: str, repo_name: str, paths: ProjectPaths) -> None:
    """Fetch PR review comments and send the engineer to address them.

    Collects feedback on the PR (top-level reviews, inline code comments,
    general conversation comments, review decision) and filters out any
    comments that were already addressed in a previous fix round.  The
    addressed-comments manifest at ``<repo>-addressed-comments.json``
    tracks comment IDs across invocations so that re-running ``--fix-pr``
    after a crash or manual restart only presents *new* feedback to the
    engineer agent.
    """
    import json as _json

    update_repo_step(slug, repo_name, "fix-pr", paths)
    wt_path = paths.worktree_path(slug, repo_name)
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"
    log_file = paths.logs_dir(slug) / f"{repo_name}-pr-fix.log"
    feedback_file = paths.spec_file(slug, f"{repo_name}-pr-feedback.md")

    # ── 0. Rebase onto the latest base branch ─────────────────────────
    rebased = step_rebase_on_base(slug, repo_name, paths)
    if not rebased:
        print(
            f"  [{repo_name}] Rebase failed — continuing with PR feedback "
            f"anyway (branch may be behind).",
            file=sys.stderr,
        )

    # ── 1. Fetch PR metadata, reviews, and general comments ───────────
    print(f"  [{repo_name}] Fetching PR review comments...")
    pr_data = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            "--json",
            "title,url,number,reviews,comments",
        ],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if pr_data.returncode != 0:
        print(
            f"  [{repo_name}] No PR found or gh error: {pr_data.stderr.strip()[:100]}"
        )
        update_repo_step(slug, repo_name, "done", paths)
        return

    try:
        data = _json.loads(pr_data.stdout)
    except _json.JSONDecodeError:
        print(f"  [{repo_name}] Could not parse PR data.")
        update_repo_step(slug, repo_name, "done", paths)
        return

    # ── 2. Fetch review decision ──────────────────────────────────────
    decision_result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            "--json",
            "reviewDecision",
            "--jq",
            ".reviewDecision",
        ],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    review_decision = (
        decision_result.stdout.strip() if decision_result.returncode == 0 else ""
    )

    # ── 3. Fetch inline review comments (file/line-level) ────────────
    #    These are NOT included in `gh pr view --json reviews`.
    #    Use the REST API: GET /repos/{owner}/{repo}/pulls/{number}/comments
    pr_number = data.get("number")
    inline_comments: list[dict] = []
    if pr_number:
        inline_result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                "repos/{owner}/{repo}/pulls/" + str(pr_number) + "/comments",
            ],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if inline_result.returncode == 0 and inline_result.stdout.strip():
            try:
                inline_comments = _json.loads(inline_result.stdout)
                if not isinstance(inline_comments, list):
                    inline_comments = []
            except _json.JSONDecodeError:
                inline_comments = []

    # ── 4. Load addressed-comments manifest & filter ────────────────
    manifest = _load_addressed_manifest(slug, repo_name, paths)
    addressed_ids = _all_addressed_ids(manifest)
    commit_before = _get_head_sha(wt_path)

    # IDs of comments included in *this* fix round (recorded after push).
    round_comment_ids: list[str] = []

    # ── 5. Build feedback markdown (new comments only) ────────────────
    reviews = data.get("reviews") or []
    comments = data.get("comments") or []

    total_reviews = len(reviews)

    lines = [
        f"# PR Review Feedback: {repo_name}",
        f"",
        f"**PR:** {data.get('url', 'N/A')}",
        f"**Title:** {data.get('title', 'N/A')}",
        f"",
    ]

    if review_decision:
        lines.append(f"**Review Decision:** {review_decision}")
        lines.append("")

    # Top-level review bodies (approve / changes-requested summaries).
    new_reviews: list[dict] = []
    for review in reviews:
        review_id = str(review.get("id", ""))
        if review_id and review_id in addressed_ids:
            continue
        new_reviews.append(review)

    if new_reviews:
        lines.append("## Reviews")
        lines.append("")
        for review in new_reviews:
            review_id = str(review.get("id", ""))
            if review_id:
                round_comment_ids.append(review_id)
            author = review.get("author", {}).get("login", "unknown")
            state = review.get("state", "")
            body = review.get("body", "").strip()
            lines.append(f"### @{author} ({state})")
            if body:
                lines.append("")
                lines.append(body)
            lines.append("")

    # Inline review comments (file/line-level code feedback).
    # Filter empty-body comments first (they never count as feedback).
    inline_with_body = [ic for ic in inline_comments if ic.get("body", "").strip()]
    total_inline = len(inline_with_body)

    new_inline: list[dict] = []
    for ic in inline_with_body:
        ic_id = str(ic.get("id", ""))
        if ic_id and ic_id in addressed_ids:
            continue
        new_inline.append(ic)

    if new_inline:
        lines.append("## Inline Code Comments")
        lines.append("")
        lines.append(
            "These are comments left on specific files and lines. Address each one."
        )
        lines.append("")
        for ic in new_inline:
            ic_id = str(ic.get("id", ""))
            if ic_id:
                round_comment_ids.append(ic_id)
            author = ic.get("user", {}).get("login", "unknown")
            file_path = ic.get("path", "unknown")
            line = ic.get("original_line") or ic.get("line") or "?"
            body = ic.get("body", "").strip()
            diff_hunk = ic.get("diff_hunk", "").strip()
            lines.append(f"### `{file_path}` (line {line}) — @{author}")
            if diff_hunk:
                lines.append("")
                lines.append("```diff")
                # Show only the last few lines of the hunk for context.
                hunk_lines = diff_hunk.splitlines()
                for hl in hunk_lines[-8:]:
                    lines.append(hl)
                lines.append("```")
            lines.append("")
            lines.append(body)
            lines.append("")

    # General conversation comments (not attached to specific lines).
    # Filter empty-body comments first.
    general_with_body = [c for c in comments if c.get("body", "").strip()]
    total_general = len(general_with_body)

    new_general: list[dict] = []
    for comment in general_with_body:
        comment_id = str(comment.get("id", ""))
        if comment_id and comment_id in addressed_ids:
            continue
        new_general.append(comment)

    if new_general:
        lines.append("## General Comments")
        lines.append("")
        for comment in new_general:
            comment_id = str(comment.get("id", ""))
            if comment_id:
                round_comment_ids.append(comment_id)
            author = comment.get("author", {}).get("login", "unknown")
            body = comment.get("body", "").strip()
            lines.append(f"**@{author}:** {body}")
            lines.append("")

    # Add a note about previously addressed comments (if any were filtered).
    skipped = (
        (total_reviews - len(new_reviews))
        + (total_inline - len(new_inline))
        + (total_general - len(new_general))
    )
    if skipped:
        lines.insert(
            5, f"**Note:** {skipped} previously addressed comment(s) omitted.\n"
        )

    feedback_content = "\n".join(lines)
    feedback_file.write_text(feedback_content, encoding="utf-8")

    new_count = len(new_reviews) + len(new_inline) + len(new_general)
    print(
        f"  [{repo_name}] Feedback: "
        f"{len(new_reviews)}/{total_reviews} reviews, "
        f"{len(new_inline)}/{total_inline} inline, "
        f"{len(new_general)}/{total_general} general "
        f"({skipped} previously addressed)"
    )

    if new_count == 0:
        print(f"  [{repo_name}] No new review comments to address — skipping agent.")
        update_repo_step(slug, repo_name, "done", paths)
        return

    # ── 6. Send engineer to fix ───────────────────────────────────────
    test_note = ""
    if repo_info and repo_info.test_hints:
        test_note = f" TESTING: {repo_info.test_hints}"

    repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)
    message = (
        f"Address ALL the PR review feedback in the attached file for this {lang} project. "
        f"Read each review comment AND each inline code comment carefully and "
        f"make the requested changes. Pay special attention to the inline code "
        f"comments in the 'Inline Code Comments' section — these point to "
        f"specific files and lines that need changes. "
        f"Commit your fixes in atomic commits. "
        f"NEVER run the full test suite. Only run the specific test files "
        f"related to the files you changed. The full suite runs in CI."
        f"{test_note}{repo_scope}"
    )

    file_args: list[str] = ["-f", str(feedback_file)]
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    if spec_file.exists():
        file_args += ["-f", str(spec_file)]

    prompt_file = paths.logs_dir(slug) / f"{repo_name}-pr-fix-prompt.md"
    _write_prompt(prompt_file, message, phase="pr-fix")
    _run_opencode(wt_path, prompt_file, file_args, log_file)

    # Push the fixes.
    print(f"  [{repo_name}] Pushing fixes...")
    push_result = subprocess.run(
        ["git", "push"], cwd=str(wt_path), capture_output=True, text=True
    )
    if push_result.returncode != 0:
        print(
            f"  [{repo_name}] git push failed after PR fix: "
            f"{push_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
    else:
        print(f"  [{repo_name}] PR fixes pushed.")

    # ── 6b. Reply to addressed comments & resolve threads ─────────────
    commit_after = _get_head_sha(wt_path)
    if push_result.returncode == 0 and pr_number and commit_after:
        _reply_and_resolve_comments(
            wt_path=wt_path,
            pr_number=pr_number,
            new_inline=new_inline,
            new_general=new_general,
            commit_sha=commit_after,
            repo_name=repo_name,
        )

    # ── 7. Record addressed comments in manifest ─────────────────────
    if round_comment_ids:
        from datetime import datetime, timezone

        manifest["fix_rounds"].append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "commit_before": commit_before,
                "commit_after": commit_after,
                "comment_ids": round_comment_ids,
            }
        )
        _save_addressed_manifest(slug, repo_name, paths, manifest)
        print(
            f"  [{repo_name}] Recorded {len(round_comment_ids)} addressed "
            f"comment(s) in manifest."
        )

    update_repo_step(slug, repo_name, "done", paths)


# ── Step: Fix cross-review findings ───────────────────────────────────


def _filter_cross_review_for_repo(
    cross_review_file: Path,
    repo_name: str,
    output_file: Path,
) -> bool:
    """Extract only the sections of cross-review.md relevant to *repo_name*.

    Scans the cross-review markdown for sections (headings, table rows,
    bullet points) that mention the repo name and writes a filtered
    version to *output_file*.  Returns True if any relevant content was
    found.
    """
    if not cross_review_file.exists():
        return False

    content = cross_review_file.read_text(encoding="utf-8")
    lines = content.splitlines()

    filtered: list[str] = [
        f"# Cross-Review Findings for {repo_name}",
        "",
        "Extracted from the full cross-repository review.",
        "",
    ]

    # Pass 1: Collect heading-delimited sections that mention the repo.
    current_section: list[str] = []
    current_heading = ""
    relevant_sections: list[tuple[str, list[str]]] = []

    for line in lines:
        if line.startswith("#"):
            # Flush previous section if relevant.
            if current_section and repo_name in "\n".join(current_section):
                relevant_sections.append((current_heading, current_section))
            current_heading = line
            current_section = [line]
        else:
            current_section.append(line)

    # Flush last section.
    if current_section and repo_name in "\n".join(current_section):
        relevant_sections.append((current_heading, current_section))

    if relevant_sections:
        for _heading, section_lines in relevant_sections:
            filtered.extend(section_lines)
            filtered.append("")
    else:
        # Fallback: include any individual lines that mention the repo.
        for line in lines:
            if repo_name in line:
                filtered.append(line)
        if len(filtered) <= 4:
            # Nothing found at all.
            return False

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(filtered), encoding="utf-8")
    return True


def step_fix_cross_review(slug: str, repo_name: str, paths: ProjectPaths) -> None:
    """Read cross-review findings and send the engineer to fix repo-specific issues."""
    wt_path = paths.worktree_path(slug, repo_name)
    cross_review_file = paths.spec_file(slug, "cross-review.md")
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    log_file = paths.logs_dir(slug) / f"{repo_name}-xfix.log"
    repo_info = REPO_BY_NAME.get(repo_name)
    lang = repo_info.language if repo_info else "unknown"

    if not cross_review_file.exists():
        print(f"  [{repo_name}] No cross-review file found, skipping.")
        return

    # Pre-filter cross-review findings to only include this repo's issues.
    filtered_file = paths.spec_file(slug, f"{repo_name}-xreview-filtered.md")
    if not _filter_cross_review_for_repo(cross_review_file, repo_name, filtered_file):
        print(f"  [{repo_name}] No cross-review findings for this repo, skipping.")
        return

    file_args: list[str] = ["-f", str(filtered_file)]
    if spec_file.exists():
        file_args += ["-f", str(spec_file)]

    test_note = ""
    if repo_info and repo_info.test_hints:
        test_note = f" TESTING: {repo_info.test_hints}"

    repo_scope = _REPO_SCOPE_INSTRUCTION.format(repo_name=repo_name)
    message = (
        f"The cross-repository review found issues that need fixing in this "
        f"{lang} repository ({repo_name}). The attached file contains ONLY "
        f"the findings relevant to this repository. Fix each finding. "
        f"Commit your fixes in atomic commits and push. "
        f"NEVER run the full test suite. Only run the specific test files "
        f"related to the files you changed. The full suite runs in CI."
        f"{test_note}{repo_scope}"
    )

    print(f"  [{repo_name}] Fixing cross-review findings...")
    prompt_file = paths.logs_dir(slug) / f"{repo_name}-xfix-prompt.md"
    _write_prompt(prompt_file, message, phase="xfix")
    _run_opencode(wt_path, prompt_file, file_args, log_file)

    # Rebase onto the latest base branch before pushing.
    step_rebase_on_base(slug, repo_name, paths)

    # Push the fixes.
    print(f"  [{repo_name}] Pushing cross-review fixes...")
    push_result = subprocess.run(
        ["git", "push"], cwd=str(wt_path), capture_output=True, text=True
    )
    if push_result.returncode != 0:
        print(
            f"  [{repo_name}] git push failed after cross-review fix: "
            f"{push_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
    else:
        print(f"  [{repo_name}] Cross-review fixes pushed.")


def run_cross_review_fix_pipeline(
    slug: str,
    repo_name: str,
) -> None:
    """Run the cross-review fix pipeline for a single repo.

    Called from a tmux pane by the orchestrator. Sequences:
    fix cross-review -> CI watch -> done.
    """
    paths = get_project_paths()

    print(f"\n{'=' * 50}")
    print(f"  [{repo_name}] Starting cross-review fix pipeline")
    print(f"{'=' * 50}\n")

    update_repo_step(slug, repo_name, "xfix", paths)
    step_fix_cross_review(slug, repo_name, paths)

    # CI watch after the fix.
    print(f"\n  [{repo_name}] -> ci-watch (post cross-review fix)")
    update_repo_step(slug, repo_name, "xfix-ci", paths)
    step_ci_watch(slug, repo_name, paths)

    print(f"\n  [{repo_name}] Cross-review fix pipeline complete.")
    update_repo_step(slug, repo_name, "xfix-done", paths)


# ── Fix PR pipeline (single repo, run from tmux pane) ─────────────────


def run_fix_pr_pipeline(
    slug: str,
    repo_name: str,
) -> None:
    """Run the fix-PR pipeline for a single repo.

    Called from a tmux pane by the dashboard or CLI. Sequences:
    fetch PR feedback -> engineer fix -> push -> CI watch -> done.
    """
    paths = get_project_paths()

    print(f"\n{'=' * 50}")
    print(f"  [{repo_name}] Starting fix-PR pipeline")
    print(f"{'=' * 50}\n")

    step_fix_pr(slug, repo_name, paths)

    # Watch CI after pushing PR fixes, consistent with cross-review-fix.
    print(f"\n  [{repo_name}] -> ci-watch (post PR fix)")
    update_repo_step(slug, repo_name, "pr-fix-ci", paths)
    step_ci_watch(slug, repo_name, paths)

    print(f"\n  [{repo_name}] Fix-PR pipeline complete.")
    update_repo_step(slug, repo_name, "done", paths)


# ── Main pipeline ─────────────────────────────────────────────────────


def run_repo_pipeline(
    slug: str,
    repo_name: str,
    *,
    max_review_rounds: int = MAX_REVIEW_ROUNDS,
) -> None:
    """Run the full build pipeline for a single repo."""
    paths = get_project_paths()

    print(f"\n{'=' * 50}")
    print(f"  [{repo_name}] Starting build pipeline")
    print(f"{'=' * 50}\n")

    # For simple bugs (no spec file), run a quick investigation step
    # so the engineer has focused context rather than a raw issue body.
    spec_file = paths.spec_file(slug, f"{repo_name}-spec.md")
    if not spec_file.exists():
        print(f"\n  [{repo_name}] -> investigate (simple bug)")
        update_repo_step(slug, repo_name, "investigate", paths)
        step_investigate(slug, repo_name, paths)

    # Engineer + build-check + review loop.
    #
    # The loop only triggers a fix round when the review contains
    # BLOCKER or MAJOR issues.  MINOR-only reviews proceed directly
    # to PR creation (non-draft) to avoid wasteful AI-to-AI cycles
    # that produce diminishing returns.
    #
    # A lightweight build-check step runs the test suite between
    # engineer and review.  If tests fail, the engineer gets one
    # immediate retry with the failure output before the review
    # cycle begins.
    review_passed = False
    verdict: ReviewVerdict | None = None
    for review_round in range(max_review_rounds):
        is_fix = review_round > 0
        step_label = f"engineer-fix-{review_round}" if is_fix else "engineer"

        print(f"\n  [{repo_name}] -> {step_label}")
        update_repo_step(slug, repo_name, step_label, paths)
        step_engineer(slug, repo_name, paths, is_fix_round=is_fix)

        # Pre-review build check: run tests before wasting a review cycle.
        print(f"\n  [{repo_name}] -> build-check")
        update_repo_step(slug, repo_name, "build-check", paths)
        if not step_build_check(slug, repo_name, paths):
            # Tests failed.  Give the engineer one shot to fix with the
            # failure output as context, then proceed to review regardless.
            print(f"  [{repo_name}] Tests failed, running engineer fix...")
            update_repo_step(slug, repo_name, "build-fix", paths)
            step_engineer(slug, repo_name, paths, is_fix_round=True)

        # Deterministic validators (agent-pragma).  HARD violations
        # trigger an engineer fix round on the same budget as the
        # review loop so the fix-then-validate cycle stays bounded.
        print(f"\n  [{repo_name}] -> pragma-validate")
        update_repo_step(slug, repo_name, "pragma-validate", paths)
        if not step_pragma_validate(slug, repo_name, paths):
            print(
                f"  [{repo_name}] Pragma HARD violations found, running engineer fix..."
            )
            update_repo_step(slug, repo_name, "pragma-fix", paths)
            step_engineer(slug, repo_name, paths, is_fix_round=True)
            # Re-validate once; still non-blocking for the review step.
            step_pragma_validate(slug, repo_name, paths)

        print(f"\n  [{repo_name}] -> review (round {review_round + 1})")
        update_repo_step(slug, repo_name, f"review-{review_round + 1}", paths)
        verdict = step_review(
            slug,
            repo_name,
            paths,
            is_followup=is_fix,
        )

        print(
            f"  [{repo_name}] Review: {verdict.status} "
            f"(B:{verdict.blockers} M:{verdict.majors} m:{verdict.minors})"
        )

        if verdict.passed:
            review_passed = True
            update_repo_step(slug, repo_name, "review-passed", paths)
            break

        if not verdict.has_blocking_issues:
            # Only MINOR issues -- not worth another engineer round.
            print(
                f"  [{repo_name}] Only minor issues found; "
                f"skipping fix round, proceeding to PR."
            )
            review_passed = True
            update_repo_step(slug, repo_name, "review-passed", paths)
            break

        print(f"  [{repo_name}] Review: blocking issues found, fixing...")

    if not review_passed:
        print(
            f"  [{repo_name}] [WARN] Review never passed after "
            f"{max_review_rounds} rounds. Creating DRAFT PR for human review.",
            file=sys.stderr,
        )
        update_repo_step(slug, repo_name, "review-not-passed", paths)

    # PR creation -- draft if the review never passed.
    pr_label = "pr (draft)" if not review_passed else "pr"
    print(f"\n  [{repo_name}] -> {pr_label}")
    update_repo_step(slug, repo_name, "pr", paths)
    step_pr(slug, repo_name, paths, draft=not review_passed)

    # CI watch + fix.
    print(f"\n  [{repo_name}] -> ci-watch")
    update_repo_step(slug, repo_name, "ci-watch", paths)
    step_ci_watch(slug, repo_name, paths)

    # Done.
    print(f"\n  [{repo_name}] Build pipeline complete.")
    update_repo_step(slug, repo_name, "done", paths)


# ── CLI entry point ───────────────────────────────────────────────────
# Primary dispatch is via ``totomisu _repo-runner`` (see cli.py).
# This block is kept as a fallback for ``python -m totomisu.repo_runner``.
if __name__ == "__main__":
    flag = sys.argv[3] if len(sys.argv) >= 4 else None
    if flag == "--fix-cross-review":
        run_cross_review_fix_pipeline(sys.argv[1], sys.argv[2])
    elif flag == "--fix-pr":
        run_fix_pr_pipeline(sys.argv[1], sys.argv[2])
    elif flag == "--ci-check":
        paths = get_project_paths()
        update_repo_step(sys.argv[1], sys.argv[2], "ci-watch", paths)
        step_ci_watch(sys.argv[1], sys.argv[2], paths)
        update_repo_step(sys.argv[1], sys.argv[2], "done", paths)
    elif flag == "--rebase":
        paths = get_project_paths()
        _slug, _repo = sys.argv[1], sys.argv[2]
        print(f"  [{_repo}] Starting rebase onto base branch...")
        ok = step_rebase_on_base(_slug, _repo, paths)
        if ok:
            print(f"  [{_repo}] Rebase completed successfully.")
        else:
            print(f"  [{_repo}] Rebase failed.", file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 3:
        run_repo_pipeline(sys.argv[1], sys.argv[2])
    else:
        print(
            f"Usage: totomisu _repo-runner <slug> <repo-name> "
            f"[--fix-cross-review|--fix-pr|--ci-check|--rebase]",
            file=sys.stderr,
        )
        sys.exit(1)
