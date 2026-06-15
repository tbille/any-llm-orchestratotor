"""Web dashboard for monitoring totomisu orchestrator progress.

Usage:
    totomisu dashboard               # start on port 8080
    totomisu dashboard --port 9090   # custom port

Frontend assets are bundled with the package and served as static files.
No build step is required.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import unquote

from totomisu.config import get_package_data_path, get_project_paths
from totomisu.costs import get_feature_costs
from totomisu.status import (
    ALL_PHASES,
    PHASE_LABELS,
    PHASES_BY_TYPE,
    cancel_feature,
    get_live_tmux_sessions,
    get_log_tails,
    get_pr_info_for_feature,
    load_all_statuses,
    load_status,
)


# ── Path constants ────────────────────────────────────────────────────

_DASHBOARD_DIR = get_package_data_path() / "dashboard"

# ANSI escape code pattern for stripping terminal colors from log lines.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── Selective caching ─────────────────────────────────────────────────
#
# Split into two tiers:
#   - Fast (5s):  status.json, tmux sessions, log tails -- cheap
#   - Slow (30s): PR/CI info, costs -- requires gh CLI calls
#
# The /api/status response merges both, but expensive data is
# refreshed less frequently.

_cache_lock = threading.Lock()
_fast_cache: dict = {}
_fast_cache_ts: float = 0.0
_FAST_TTL = 5.0

_slow_cache: dict = {}
_slow_cache_ts: float = 0.0
_SLOW_TTL = 30.0


def _build_fast_data() -> tuple[list[dict], dict[str, list[str]]]:
    """Collect cheap data: statuses, tmux sessions, log tails."""
    paths = get_project_paths()
    features = load_all_statuses(paths)
    tmux = get_live_tmux_sessions()

    for feat in features:
        slug = feat.get("slug", "")
        feat["tmux_sessions"] = tmux.get(slug, [])
        feat["log_tails"] = get_log_tails(slug, feat.get("repos", []), paths)

    return features, tmux


def _build_slow_data(features: list[dict]) -> None:
    """Enrich features with expensive data: PR/CI info, costs."""
    paths = get_project_paths()
    for feat in features:
        slug = feat.get("slug", "")
        current = feat.get("current_phase", "")
        if (
            current in ("build", "cross-review", "cross-review-fix")
            or feat.get("phases", {}).get("build", {}).get("status") == "done"
        ):
            feat["pr_info"] = get_pr_info_for_feature(
                slug, feat.get("repos", []), paths
            )
        else:
            feat["pr_info"] = {}
        feat["costs"] = get_feature_costs(slug, paths) or {}


def _build_api_response() -> dict:
    """Collect all data the dashboard needs in a single JSON payload."""
    global _fast_cache, _fast_cache_ts, _slow_cache, _slow_cache_ts

    now = time.monotonic()

    with _cache_lock:
        fast_expired = not _fast_cache or (now - _fast_cache_ts) >= _FAST_TTL
        slow_expired = not _slow_cache or (now - _slow_cache_ts) >= _SLOW_TTL

    if fast_expired:
        features, tmux = _build_fast_data()

        if slow_expired:
            _build_slow_data(features)
            with _cache_lock:
                _slow_cache = {
                    f.get("slug", ""): {
                        "pr_info": f.get("pr_info", {}),
                        "costs": f.get("costs", {}),
                    }
                    for f in features
                }
                _slow_cache_ts = now
        else:
            # Merge cached slow data into fresh fast data.
            with _cache_lock:
                for feat in features:
                    slug = feat.get("slug", "")
                    cached = _slow_cache.get(slug, {})
                    feat.setdefault("pr_info", cached.get("pr_info", {}))
                    feat.setdefault("costs", cached.get("costs", {}))

        result = {
            "features": features,
            "phase_labels": PHASE_LABELS,
            "phases_by_type": {k: list(v) for k, v in PHASES_BY_TYPE.items()},
            "all_phases": list(ALL_PHASES),
        }

        with _cache_lock:
            _fast_cache = result
            _fast_cache_ts = now

        return result

    with _cache_lock:
        return _fast_cache


def _invalidate_cache() -> None:
    """Force both cache tiers to expire on next poll."""
    global _fast_cache, _fast_cache_ts, _slow_cache, _slow_cache_ts
    with _cache_lock:
        _fast_cache = {}
        _fast_cache_ts = 0.0
        _slow_cache = {}
        _slow_cache_ts = 0.0


# ── Document API ──────────────────────────────────────────────────────


def _build_docs_response(slug: str) -> dict | None:
    """Collect all documents for a single feature, grouped by category."""
    paths = get_project_paths()
    spec_dir = paths.spec_dir(slug)
    if not spec_dir.exists():
        return None

    status = load_status(slug, paths)
    triage_type = status.get("triage_type", "unknown") if status else "unknown"
    repos = status.get("repos", []) if status else []
    current_phase = status.get("current_phase", "") if status else ""

    group_labels = {
        "requirements": "Requirements & Design",
        "specs": "Technical Specifications",
        "reviews": "Code Reviews",
        "ci_and_pr": "CI & PR Feedback",
        "other": "Other",
    }
    groups: dict[str, list[dict]] = {k: [] for k in group_labels}

    known_labels: dict[str, tuple[str, str, int]] = {
        "input.md": ("requirements", "Original input", 0),
        "prd.md": ("requirements", "PRD", 1),
        "design.md": ("requirements", "Design proposal", 2),
        "triage.json": ("requirements", "Triage classification", 3),
        "tech-spec.md": ("specs", "Overall tech spec", 0),
        "cross-review.md": ("reviews", "Cross-repo review", 100),
    }

    def classify(name: str) -> tuple[str, str, int]:
        if name in known_labels:
            return known_labels[name]
        if name.endswith("-spec.md"):
            repo = name.replace("-spec.md", "")
            return ("specs", f"{repo} spec", 10)
        if name.endswith("-review.md"):
            repo = name.replace("-review.md", "")
            return ("reviews", f"{repo} code review", 10)
        if name.endswith("-ci-failures.md"):
            repo = name.replace("-ci-failures.md", "")
            return ("ci_and_pr", f"{repo} CI failures", 10)
        if name.endswith("-pr-feedback.md"):
            repo = name.replace("-pr-feedback.md", "")
            return ("ci_and_pr", f"{repo} PR feedback", 20)
        if name.endswith("-investigation.md"):
            repo = name.replace("-investigation.md", "")
            return ("specs", f"{repo} investigation", 5)
        if name.endswith("-build-failures.md"):
            repo = name.replace("-build-failures.md", "")
            return ("ci_and_pr", f"{repo} build failures", 5)
        if name.endswith("-xreview-filtered.md"):
            repo = name.replace("-xreview-filtered.md", "")
            return ("reviews", f"{repo} cross-review (filtered)", 50)
        if name.endswith("-addressed-comments.json"):
            repo = name.replace("-addressed-comments.json", "")
            return ("ci_and_pr", f"{repo} addressed comments", 30)
        if name in ("status.json", "costs.json"):
            return ("other", name, 0)
        if name == "debate-done":
            return ("other", "Debate marker", 10)
        return ("other", name, 50)

    for entry in sorted(spec_dir.iterdir()):
        if entry.is_dir():
            continue
        name = entry.name
        if not (
            name.endswith(".md") or name.endswith(".json") or name == "debate-done"
        ):
            continue
        group_key, label, sort_order = classify(name)
        stat = entry.stat()
        groups[group_key].append(
            {
                "name": name,
                "label": label,
                "sort_order": sort_order,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    for docs in groups.values():
        docs.sort(key=lambda d: (d["sort_order"], d["name"]))

    return {
        "slug": slug,
        "triage_type": triage_type,
        "repos": repos,
        "current_phase": current_phase,
        "groups": groups,
        "group_labels": group_labels,
    }


def _read_doc_content(slug: str, filename: str) -> str | None:
    """Read a single document file, with path traversal protection."""
    paths = get_project_paths()
    spec_dir = paths.spec_dir(slug)
    target = (spec_dir / filename).resolve()

    if not str(target).startswith(str(spec_dir.resolve())):
        return None
    if not target.suffix in (".md", ".json") and target.name != "debate-done":
        return None
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


# ── Log API (full tail + SSE streaming) ───────────────────────────────


def _get_log_path(slug: str, repo_name: str) -> Path | None:
    """Find the most recent log file for a repo."""
    paths = get_project_paths()
    logs_dir = paths.logs_dir(slug)
    if not logs_dir.exists():
        return None

    # Check in priority order (most recent phase first).
    for phase in ("ci-fix", "pr-fix", "pr", "review", "engineer", "investigate"):
        log_file = logs_dir / f"{repo_name}-{phase}.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            return log_file
    return None


def _read_log_tail(log_path: Path, lines: int = 200) -> str:
    """Read the last N lines of a log file, stripping ANSI codes."""
    try:
        size = log_path.stat().st_size
        if size == 0:
            return ""
        read_size = min(size, lines * 500)  # rough estimate
        with log_path.open("rb") as fh:
            fh.seek(max(0, size - read_size))
            chunk = fh.read(read_size).decode("utf-8", errors="replace")
        all_lines = chunk.splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "\n".join(_ANSI_RE.sub("", line) for line in tail)
    except OSError:
        return ""


# ── Static file serving ───────────────────────────────────────────────


_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".m4a": "audio/mp4",
}


def _serve_static(path: str) -> tuple[bytes, str, int] | None:
    """Resolve a /static/... path to a file in the dashboard/ directory.

    Returns ``(content, content_type, status_code)`` or ``None`` if not found.
    """
    # Strip the /static/ prefix.
    rel = path[len("/static/") :]
    target = (_DASHBOARD_DIR / rel).resolve()

    # Path traversal protection (trailing slash prevents prefix false match).
    if not str(target).startswith(str(_DASHBOARD_DIR.resolve()) + os.sep):
        return None
    if not target.is_file():
        return None

    ext = target.suffix.lower()
    content_type = _MIME_TYPES.get(ext, "application/octet-stream")
    try:
        content = target.read_bytes()
        return content, content_type, 200
    except OSError:
        return None


# ── HTTP Handler ──────────────────────────────────────────────────────


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # Static files.
        if self.path.startswith("/static/"):
            result = _serve_static(self.path)
            if result:
                content, content_type, status = result
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)
            return

        # API endpoints.
        if self.path == "/api/status":
            self._json_response(_build_api_response())
        elif self.path.startswith("/api/docs/"):
            self._handle_docs_api()
        elif self.path.startswith("/api/logs/"):
            self._handle_logs_api()
        # HTML pages (serve from dashboard/ directory).
        elif self.path.startswith("/docs/"):
            self._serve_html("docs.html")
        elif self.path in ("/", "/index.html"):
            self._serve_html("index.html")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/fix-prs":
            self._handle_fix_prs()
        elif self.path == "/api/stop-fix-prs":
            self._handle_stop_fix_prs()
        elif self.path == "/api/ci-check":
            self._handle_ci_check()
        elif self.path == "/api/resume":
            self._handle_resume()
        elif self.path == "/api/rebase":
            self._handle_rebase()
        elif self.path == "/api/cancel":
            self._handle_cancel()
        else:
            self.send_error(404)

    # ── HTML serving ──────────────────────────────────────

    def _serve_html(self, filename: str) -> None:
        """Serve an HTML file from the dashboard/ directory."""
        target = _DASHBOARD_DIR / filename
        if not target.is_file():
            self.send_error(404)
            return
        try:
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_error(500)

    # ── Docs API ──────────────────────────────────────────

    def _handle_docs_api(self) -> None:
        rest = unquote(self.path[len("/api/docs/") :])
        parts = rest.split("/", 1)
        slug = parts[0]
        filename = parts[1] if len(parts) > 1 else None

        if not slug:
            self._json_status(400, {"error": "Missing slug"})
            return

        if filename:
            content = _read_doc_content(slug, filename)
            if content is None:
                self._json_status(404, {"error": f"Document '{filename}' not found"})
                return
            body = content.encode("utf-8")
            ct = (
                "application/json"
                if filename.endswith(".json")
                else "text/plain; charset=utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            data = _build_docs_response(slug)
            if data is None:
                self._json_status(404, {"error": f"Feature '{slug}' not found"})
                return
            self._json_response(data)

    # ── Logs API (REST + SSE) ─────────────────────────────

    def _handle_logs_api(self) -> None:
        """Handle /api/logs/<slug>/<repo>/stream (SSE) and /api/logs/<slug>/<repo> (REST)."""
        rest = unquote(self.path[len("/api/logs/") :])
        parts = rest.split("/")

        if len(parts) < 2:
            self._json_status(400, {"error": "Missing slug or repo"})
            return

        slug, repo = parts[0], parts[1]
        is_stream = len(parts) >= 3 and parts[2] == "stream"

        log_path = _get_log_path(slug, repo)
        if log_path is None:
            if is_stream:
                self._json_status(404, {"error": "No log file found"})
            else:
                self._json_status(404, {"error": "No log file found"})
            return

        if is_stream:
            self._handle_log_stream(log_path)
        else:
            lines = 200
            content = _read_log_tail(log_path, lines)
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _handle_log_stream(self, log_path: Path) -> None:
        """SSE endpoint that tails a log file in real-time."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            # Send initial content.
            initial = _read_log_tail(log_path, 200)
            self.wfile.write(f"event: initial\ndata: {initial}\n\n".encode())
            self.wfile.flush()

            # Tail the file for new content.
            last_size = log_path.stat().st_size
            idle_count = 0
            idle_notified = False
            max_idle = 300  # 5 minutes of inactivity => disconnect

            while True:
                time.sleep(1)
                try:
                    current_size = log_path.stat().st_size
                except OSError:
                    break

                if current_size > last_size:
                    idle_count = 0
                    idle_notified = False
                    with log_path.open("rb") as fh:
                        fh.seek(last_size)
                        new_data = fh.read(current_size - last_size).decode(
                            "utf-8", errors="replace"
                        )
                    last_size = current_size
                    for line in new_data.splitlines():
                        cleaned = _ANSI_RE.sub("", line)
                        if cleaned.strip():
                            self.wfile.write(
                                f"event: append\ndata: {cleaned}\n\n".encode()
                            )
                    self.wfile.flush()
                else:
                    idle_count += 1
                    if idle_count >= 30 and not idle_notified:
                        self.wfile.write(b"event: idle\ndata: \n\n")
                        self.wfile.flush()
                        idle_notified = True
                    if idle_count >= max_idle:
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected.

    # ── Action endpoints ──────────────────────────────────

    def _read_json_body(self) -> dict | None:
        """Read and parse JSON from the request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json_status(400, {"error": "Invalid JSON"})
            return None

    def _handle_fix_prs(self) -> None:
        from totomisu.engineer import run_fix_pr_pipelines

        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        repo = payload.get("repo")  # Optional: specific repo
        if not slug:
            self._json_status(400, {"error": "Missing 'slug' field"})
            return

        paths = get_project_paths()
        status = load_status(slug, paths)
        if status is None:
            self._json_status(404, {"error": f"Feature '{slug}' not found"})
            return

        repos = [repo] if repo else status.get("repos", [])
        if not repos:
            self._json_status(400, {"error": "No repos for this feature"})
            return

        run_fix_pr_pipelines(slug, repos, paths, attach=False)
        _invalidate_cache()
        self._json_status(202, {"status": "started", "slug": slug, "repos": repos})

    def _handle_stop_fix_prs(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        if not slug:
            self._json_status(400, {"error": "Missing 'slug' field"})
            return

        session_name = f"fix-pr-{slug}"
        result = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
        )
        _invalidate_cache()
        if result.returncode != 0:
            self._json_status(404, {"error": f"No active fix-pr session for '{slug}'"})
            return
        self._json_status(200, {"status": "stopped", "slug": slug})

    def _handle_ci_check(self) -> None:
        """Trigger CI re-check for a single repo via tmux."""
        import shlex

        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        repo = payload.get("repo", "")
        if not slug or not repo:
            self._json_status(400, {"error": "Missing 'slug' or 'repo'"})
            return

        paths = get_project_paths()
        wt_path = paths.worktree_path(slug, repo)
        if not wt_path.exists():
            self._json_status(404, {"error": f"Worktree not found for '{repo}'"})
            return

        session_name = f"ci-check-{slug}-{repo}"
        cmd = (
            f"totomisu _repo-runner {shlex.quote(slug)} {shlex.quote(repo)} --ci-check"
        )

        # Kill any existing session first.
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
        )

        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(paths.root),
                cmd,
            ],
            check=False,
        )
        _invalidate_cache()
        self._json_status(202, {"status": "started", "slug": slug, "repo": repo})

    def _handle_rebase(self) -> None:
        """Rebase a repo's feature branch onto the latest base branch via tmux."""
        import shlex

        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        repo = payload.get("repo", "")
        if not slug or not repo:
            self._json_status(400, {"error": "Missing 'slug' or 'repo'"})
            return

        paths = get_project_paths()
        wt_path = paths.worktree_path(slug, repo)
        if not wt_path.exists():
            self._json_status(404, {"error": f"Worktree not found for '{repo}'"})
            return

        session_name = f"rebase-{slug}-{repo}"
        cmd = f"totomisu _repo-runner {shlex.quote(slug)} {shlex.quote(repo)} --rebase"

        # Kill any existing session first.
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
        )

        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(paths.root),
                cmd,
            ],
            check=False,
        )
        _invalidate_cache()
        self._json_status(202, {"status": "started", "slug": slug, "repo": repo})

    def _handle_resume(self) -> None:
        """Resume the pipeline from a specific phase via tmux."""
        import shlex

        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        phase = payload.get("phase", "")
        if not slug or not phase:
            self._json_status(400, {"error": "Missing 'slug' or 'phase'"})
            return

        paths = get_project_paths()
        status = load_status(slug, paths)
        if status is None:
            self._json_status(404, {"error": f"Feature '{slug}' not found"})
            return

        session_name = f"resume-{slug}"
        cmd = (
            f"totomisu run --resume {shlex.quote(slug)} --skip-to {shlex.quote(phase)}"
        )

        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
        )
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(paths.root),
                cmd,
            ],
            check=False,
        )
        _invalidate_cache()
        self._json_status(202, {"status": "started", "slug": slug, "phase": phase})

    def _handle_cancel(self) -> None:
        """Cancel all running pipelines for a feature."""
        payload = self._read_json_body()
        if payload is None:
            return

        slug = payload.get("slug", "")
        if not slug:
            self._json_status(400, {"error": "Missing 'slug' field"})
            return

        paths = get_project_paths()
        sessions_to_kill = cancel_feature(slug, paths)

        killed = []
        for session_name in sessions_to_kill:
            result = subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            if result.returncode == 0:
                killed.append(session_name)

        _invalidate_cache()
        self._json_status(
            200,
            {
                "status": "cancelled",
                "slug": slug,
                "killed_sessions": killed,
            },
        )

    # ── Response helpers ──────────────────────────────────

    def _json_response(self, data: dict) -> None:
        self._json_status(200, data)

    def _json_status(self, status_code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default stderr logging for clean terminal output.
        pass


# ── Server ────────────────────────────────────────────────────────────


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread so slow API calls don't block."""

    daemon_threads = True


def run_dashboard(*, port: int = 8080) -> None:
    """Start the dashboard HTTP server.

    Called by ``totomisu dashboard`` (see cli.py).
    """
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="totomisu dashboard",
        description="Web dashboard for the totomisu orchestrator.",
    )
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()
    run_dashboard(port=args.port)


if __name__ == "__main__":
    main()
