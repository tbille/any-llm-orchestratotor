"""Phase 1: Fetch GitHub issues and triage into simple-bug / complex-bug / feature."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from totomisu.config import (
    CLASSIFIER_TIMEOUT,
    ECOSYSTEM_CONTEXT,
    REPO_BY_NAME,
    ProjectPaths,
)


# ── Data types ────────────────────────────────────────────────────────

TRIAGE_TYPES = ("simple-bug", "complex-bug", "feature")

# Spec-phase agents the classifier can select for a feature.
SPEC_PHASES = ("pm", "debate", "designer", "architect")

# Default spec phases per triage type (used when the classifier doesn't
# return an explicit phases list, or for backward-compatible resume).
_DEFAULT_PHASES: dict[str, list[str]] = {
    "feature": ["pm", "debate", "designer", "architect"],
    "complex-bug": ["architect"],
    "simple-bug": [],
}


def _normalize_phases(phases: list[str], triage_type: str) -> list[str]:
    """Validate and enforce invariants on the spec-phase list.

    - Strip unknown phase names.
    - Enforce: ``"pm"`` implies ``"debate"``; ``"debate"`` without ``"pm"``
      is removed.
    - Preserve the canonical order defined by ``SPEC_PHASES``.
    """
    valid = [p for p in phases if p in SPEC_PHASES]

    # pm <-> debate invariant
    if "pm" in valid and "debate" not in valid:
        valid.append("debate")
    if "debate" in valid and "pm" not in valid:
        valid.remove("debate")

    # Re-order to canonical sequence.
    return [p for p in SPEC_PHASES if p in valid]


@dataclass
class TriageResult:
    triage_type: str  # one of TRIAGE_TYPES
    repos: list[str]
    slug: str
    reasoning: str
    raw_input: str  # original issue body or prompt
    phases: list[str] | None = None  # subset of SPEC_PHASES; None = infer

    def __post_init__(self) -> None:
        if self.triage_type not in TRIAGE_TYPES:
            raise ValueError(
                f"Invalid triage type {self.triage_type!r}, "
                f"expected one of {TRIAGE_TYPES}"
            )
        unknown = [r for r in self.repos if r not in REPO_BY_NAME]
        if unknown:
            raise ValueError(f"Unknown repos in triage: {unknown}")

        # Resolve phases: use explicit value, or fall back to type default.
        if self.phases is None:
            self.phases = list(_DEFAULT_PHASES.get(self.triage_type, []))
        self.phases = _normalize_phases(self.phases, self.triage_type)


# ── Issue fetching ────────────────────────────────────────────────────

_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)


def parse_issue_url(url: str) -> tuple[str, str, str]:
    """Extract (owner, repo, number) from a GitHub issue URL."""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a valid GitHub issue URL: {url}")
    return m.group("owner"), m.group("repo"), m.group("number")


def fetch_issue(url: str) -> dict:
    """Fetch a GitHub issue via ``gh`` and return structured data."""
    owner, repo, number = parse_issue_url(url)
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            number,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,body,labels,comments,state,url",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    data["source_repo"] = repo
    data["source_owner"] = owner
    return data


def format_issue_as_input(issue: dict) -> str:
    """Turn the gh JSON into a clean markdown input document."""
    lines = [
        f"# GitHub Issue: {issue['title']}",
        f"",
        f"**Source:** {issue.get('url', 'N/A')}  ",
        f"**Repository:** {issue.get('source_owner', '')}/{issue.get('source_repo', '')}  ",
        f"**State:** {issue.get('state', 'unknown')}  ",
    ]

    labels = issue.get("labels") or []
    if labels:
        label_names = ", ".join(lb.get("name", str(lb)) for lb in labels)
        lines.append(f"**Labels:** {label_names}  ")

    lines += [
        "",
        "## Description",
        "",
        issue.get("body") or "_No description provided._",
    ]

    comments = issue.get("comments") or []
    if comments:
        lines += ["", "## Comments", ""]
        for i, comment in enumerate(comments, 1):
            author = comment.get("author", {}).get("login", "unknown")
            body = comment.get("body", "")
            lines += [f"### Comment {i} (by @{author})", "", body, ""]

    return "\n".join(lines)


# ── Triage classifier ────────────────────────────────────────────────

_CLASSIFIER_PROMPT = """\
You are a triage classifier for the otari ecosystem.

{ecosystem}

## Your task

Read the input below and classify it. Respond with ONLY a JSON object, no markdown fences, no explanation outside the JSON.

```json
{{
  "type": "simple-bug" | "complex-bug" | "feature",
  "repos": ["<repo-name>", ...],
  "phases": ["<phase>", ...],
  "slug": "<short-kebab-case-slug>",
  "reasoning": "<one paragraph>"
}}
```

### Classification rules

- **simple-bug**: A clear, contained bug likely affecting a single repository.
  The fix is straightforward and does not change any cross-repo API contract.
- **complex-bug**: A bug that spans multiple repositories, has unclear root cause,
  or requires coordinated changes across repos.
- **feature**: New functionality, behavioral change, or enhancement that needs
  product requirements and possibly design work.

### Phase selection rules

The `phases` array controls which spec-phase agents run before engineering.
Choose from: `"pm"`, `"debate"`, `"designer"`, `"architect"`.

- **simple-bug**: always `[]` (no spec phases needed).
- **complex-bug**: typically `["architect"]`.
- **feature**: pick the subset that fits:
  - Include `"pm"` (and `"debate"`) when there is product ambiguity, user-facing
    scope decisions, or trade-offs that need a Product Requirements Document.
  - Include `"designer"` when the change involves user-facing API shape changes,
    SDK ergonomics, CLI/configuration UX, error message design, or documentation
    patterns. Omit it for purely internal/infrastructure changes.
  - Include `"architect"` when implementation planning is needed (almost always).
    May be omitted only for trivial single-file changes.
  - A purely technical feature (refactoring, performance, infra, CI, internal
    plumbing) may only need `["architect"]` -- skip pm/debate/designer.

**Constraint**: `"pm"` and `"debate"` are always paired. If you include `"pm"`,
also include `"debate"`. Never include `"debate"` without `"pm"`.

### Repo names (use EXACTLY these)
any-llm, otari, otari-sdk-python, otari-sdk-rust, otari-sdk-go, otari-sdk-ts, otari-ai

### Slug rules
- Lowercase kebab-case, max 40 characters
- Descriptive of the issue, e.g. "fix-streaming-timeout" or "add-batch-api"

## Input

{input_text}
"""


_CLASSIFIER_TIMEOUT = CLASSIFIER_TIMEOUT


def classify(input_text: str, paths: ProjectPaths) -> TriageResult:
    """Run the headless classifier agent and return a TriageResult."""
    prompt = _CLASSIFIER_PROMPT.format(
        ecosystem=ECOSYSTEM_CONTEXT,
        input_text=input_text,
    )

    try:
        result = subprocess.run(
            [
                "opencode",
                "run",
                "--dir",
                str(paths.root),
                "--dangerously-skip-permissions",
                "--format",
                "json",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=_CLASSIFIER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _die(
            f"Triage classifier timed out after {_CLASSIFIER_TIMEOUT}s. "
            f"Check that opencode is working correctly."
        )

    # opencode run --format json outputs newline-delimited JSON events.
    # The assistant's text reply is in message events.
    reply_text = _extract_reply(result.stdout)
    return _parse_triage_json(reply_text, input_text)


def _extract_reply(raw_output: str) -> str:
    """Pull the assistant's final text from opencode JSON event stream.

    opencode ``--format json`` emits newline-delimited JSON events.
    Text content lives at ``event["part"]["text"]`` when
    ``event["type"] == "text"``.
    """
    text_parts: list[str] = []
    for line in raw_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Might be plain text output (non-json format fallback).
            text_parts.append(line)
            continue

        if not isinstance(event, dict):
            continue

        # Primary path: opencode wraps text in event["part"]["text"].
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])

    if text_parts:
        return "\n".join(text_parts)

    # Fallback: treat entire stdout as plain text.
    return raw_output.strip()


def _parse_triage_json(text: str, raw_input: str) -> TriageResult:
    """Extract the JSON object from the classifier's reply."""
    from totomisu.parse import parse_classifier_json

    data = parse_classifier_json(text, required_keys=["type", "repos", "slug"])
    if data is None:
        _die(f"Classifier did not return valid JSON.\nRaw output:\n{text}")

    # Phases may be absent (old-style classifier response).  TriageResult
    # will fall back to _DEFAULT_PHASES[triage_type] when phases is None.
    phases = data.get("phases")
    if not isinstance(phases, list):
        phases = None

    return TriageResult(
        triage_type=data["type"],
        repos=data["repos"],
        slug=data["slug"],
        reasoning=data.get("reasoning", ""),
        raw_input=raw_input,
        phases=phases,
    )


# ── User confirmation ────────────────────────────────────────────────

_TYPE_LABELS = {
    "simple-bug": "Simple bug   (straight to engineer)",
    "complex-bug": "Complex bug  (phases selected by classifier)",
    "feature": "Feature      (phases selected by classifier)",
}


def confirm_triage(result: TriageResult) -> TriageResult:
    """Show the triage to the user and allow override."""
    phases_str = ", ".join(result.phases) if result.phases else "(none)"
    print("\n── Triage Result ──────────────────────────────────────")
    print(f"  Type:    {result.triage_type}")
    print(f"  Slug:    {result.slug}")
    print(f"  Repos:   {', '.join(result.repos)}")
    print(f"  Phases:  {phases_str}")
    print(f"  Reason:  {result.reasoning}")
    print("──────────────────────────────────────────────────────\n")

    answer = input("Accept this classification? [Y/n/change] ").strip().lower()
    if answer in ("", "y", "yes"):
        return result

    # Let the user override.
    print("\nAvailable types:")
    for key, label in _TYPE_LABELS.items():
        print(f"  {key:12s} -- {label}")
    new_type = (
        input(f"\nNew type [{result.triage_type}]: ").strip() or result.triage_type
    )

    repo_names = list(REPO_BY_NAME.keys())
    print(f"\nAvailable repos: {', '.join(repo_names)}")
    new_repos_raw = input(
        f"Repos (comma-separated) [{', '.join(result.repos)}]: "
    ).strip()
    new_repos = (
        [r.strip() for r in new_repos_raw.split(",") if r.strip()]
        if new_repos_raw
        else result.repos
    )

    print(f"\nAvailable phases: {', '.join(SPEC_PHASES)}")
    new_phases_raw = input(f"Phases (comma-separated) [{phases_str}]: ").strip()
    new_phases: list[str] | None
    if new_phases_raw:
        new_phases = [p.strip() for p in new_phases_raw.split(",") if p.strip()]
    else:
        new_phases = result.phases

    new_slug = input(f"Slug [{result.slug}]: ").strip() or result.slug

    return TriageResult(
        triage_type=new_type,
        repos=new_repos,
        slug=new_slug,
        reasoning=result.reasoning,
        raw_input=result.raw_input,
        phases=new_phases,
    )


# ── Persistence ───────────────────────────────────────────────────────


def save_input(slug: str, input_text: str, paths: ProjectPaths) -> Path:
    """Write the raw input document to specs/<slug>/input.md."""
    paths.ensure_spec_dirs(slug)
    dest = paths.spec_file(slug, "input.md")
    dest.write_text(input_text, encoding="utf-8")
    return dest


def save_triage(slug: str, triage: TriageResult, paths: ProjectPaths) -> Path:
    """Persist the triage result as JSON for resume support."""
    paths.ensure_spec_dirs(slug)
    dest = paths.spec_file(slug, "triage.json")
    dest.write_text(
        json.dumps(
            {
                "type": triage.triage_type,
                "repos": triage.repos,
                "phases": triage.phases,
                "slug": triage.slug,
                "reasoning": triage.reasoning,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest


def load_triage(slug: str, paths: ProjectPaths) -> TriageResult | None:
    """Load a previously saved triage, or None if it does not exist.

    Old triage files without a ``phases`` key are handled gracefully:
    ``TriageResult.__post_init__`` infers default phases from the
    triage type.
    """
    path = paths.spec_file(slug, "triage.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_input_path = paths.spec_file(slug, "input.md")
    raw_input = (
        raw_input_path.read_text(encoding="utf-8") if raw_input_path.exists() else ""
    )

    # phases may be absent in old triage files; None triggers default.
    phases = data.get("phases")
    if not isinstance(phases, list):
        phases = None

    return TriageResult(
        triage_type=data["type"],
        repos=data["repos"],
        slug=data["slug"],
        reasoning=data.get("reasoning", ""),
        raw_input=raw_input,
        phases=phases,
    )


# ── Helpers ───────────────────────────────────────────────────────────


def _die(msg: str) -> None:  # noqa: N802
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)
