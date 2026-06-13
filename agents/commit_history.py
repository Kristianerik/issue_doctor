"""
agents/commit_history.py - Commit history mining for retrieved files.

For each of the top retrieved files, finds recent bug-fix commits and returns
a compact summary labeled ## Prior Fixes. Works on any git repo.

Design constraints:
- Hard 15s timeout total across all git calls
- Max 1200 chars output (~300 tokens)
- Fails silently — returns empty string on any error
- Stateless, read-only
"""

import re
import subprocess
import time
from pathlib import Path


MAX_OUTPUT_CHARS = 1200
MAX_FILES = 3
TIMEOUT_TOTAL = 15.0  # seconds across all git calls


def get_commit_history(retrieved_files: list, repo_root: Path) -> str:
    """
    Mine recent bug-fix commits for the top retrieved files.

    retrieved_files: list of repo-relative filepath strings (from RAG results)
    repo_root: Path to the repo root

    Returns a string labeled ## Prior Fixes, or empty string if nothing found.
    """
    try:
        return _mine_history(retrieved_files, repo_root)
    except Exception:
        return ""


def _mine_history(retrieved_files: list, repo_root: Path) -> str:
    if not retrieved_files or not repo_root.exists():
        return ""

    # Deduplicate and cap to top MAX_FILES files
    seen = set()
    top_files = []
    for f in retrieved_files:
        if f not in seen:
            seen.add(f)
            top_files.append(f)
        if len(top_files) >= MAX_FILES:
            break

    parts = []
    deadline = time.monotonic() + TIMEOUT_TOTAL

    for filepath in top_files:
        if time.monotonic() > deadline:
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        try:
            result = subprocess.run(
                [
                    "git", "log", "--oneline",
                    "--since=6 months ago",
                    "-E",
                    "--grep=fix|crash|assert|revert|regression",
                    "-i",   # case-insensitive
                    "-5",   # max 5 commits per file
                    "--", filepath,
                ],
                capture_output=True, text=True,
                cwd=str(repo_root),
                timeout=min(remaining, 8.0),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

        if result.returncode != 0 or not result.stdout.strip():
            continue

        commits = [
            line.strip() for line in result.stdout.strip().splitlines()
            if line.strip()
        ]
        if not commits:
            continue

        # Truncate long commit messages
        short_commits = []
        for c in commits:
            short_commits.append(c[:80] + ("..." if len(c) > 80 else ""))

        basename = filepath.split("/")[-1]
        block = f"**{basename}** (`{filepath}`):\n"
        block += "\n".join(f"  - {c}" for c in short_commits)
        parts.append(block)

    if not parts:
        return ""

    result_text = "## Prior Fixes\n\n" + "\n\n".join(parts)
    return result_text[:MAX_OUTPUT_CHARS]