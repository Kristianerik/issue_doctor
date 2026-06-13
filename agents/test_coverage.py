"""
agents/test_coverage.py - Test coverage mapping using issue keywords.

Extracts user-facing keywords from the issue text (feature names, diagnostic
keywords, template syntax) and searches test directories for files containing
them. Works on any repo — keywords from issue text appear in test files
universally, unlike internal function names or source file references.

Design constraints:
- Hard 10s timeout total
- Max 400 chars output (~100 tokens)
- Fails silently — returns empty string on any error
- Stateless, read-only, no LLM calls
- Works on any repo with test directories
"""

import re
import subprocess
import time
from pathlib import Path


MAX_OUTPUT_CHARS = 400
MAX_SEARCH_TERMS = 3
MAX_TEST_FILES = 3
TIMEOUT_TOTAL = 10.0


def get_test_coverage(issue_text: str, retrieved_files: list,
                      repo_root: Path) -> str:
    """
    Find test files relevant to this issue using keywords from the issue text.

    issue_text: full issue text
    retrieved_files: list of repo-relative filepath strings (used as fallback)
    repo_root: Path to the repo root

    Returns ## Existing Tests summary or empty string if nothing found.
    """
    try:
        return _find_coverage(issue_text, retrieved_files, repo_root)
    except Exception:
        return ""


STOP_WORDS = {
    'assertion', 'failure', 'error', 'crash', 'bug', 'fix',
    'issue', 'problem', 'invalid', 'malformed', 'class', 'template',
    'function', 'method', 'variable', 'pointer', 'value', 'type',
    'false', 'true', 'null', 'none', 'test', 'check', 'case',
}


def _extract_search_terms(issue_text: str, retrieved_files: list) -> list:
    """
    Extract user-facing keywords likely to appear in test files.
    Priority: backtick identifiers → CamelCase from title → title words >6 chars.
    Preserve case — git grep runs with -i (case-insensitive).
    """
    terms = []

    # 1. Backtick identifiers — highest signal, user-facing names
    for m in re.finditer(r'`([A-Za-z][A-Za-z0-9_\-]{3,40})`', issue_text):
        t = m.group(1)
        if t.lower() not in STOP_WORDS and t not in terms:
            terms.append(t)
        if len(terms) >= MAX_SEARCH_TERMS:
            return terms

    # 2. Compound CamelCase words from the issue title only.
    # Must have at least one internal capital transition (e.g. InitListChecker)
    # Single-capital words like 'Specialization' are excluded.
    title = issue_text.split('\n')[0]
    for m in re.finditer(r'\b([A-Za-z]\w+)\b', title):
        t = m.group(1)
        if (re.search(r'[a-z][A-Z]', t)          # has internal capital transition
                and t.lower() not in STOP_WORDS
                and t not in terms):
            terms.append(t)
        if len(terms) >= MAX_SEARCH_TERMS:
            return terms

    # No fallback to generic title words — return what we have (may be empty)
    return terms


def _find_test_dirs(repo_root: Path) -> list:
    """
    Find test directories at depth 1 and 2.
    Pattern-based — works on any repo without hardcoded paths.
    """
    test_dirs = []
    try:
        entries = list(repo_root.iterdir())
    except (OSError, PermissionError):
        return []

    for d in entries:
        try:
            if not d.is_dir() or d.name.startswith('.'):
                continue
        except (OSError, PermissionError):
            continue
        if 'test' in d.name.lower() and 'build' not in d.name.lower():
            test_dirs.append(d.name)
        try:
            for sub in d.iterdir():
                try:
                    if (sub.is_dir() and 'test' in sub.name.lower()
                            and d.name.lower() != 'build'):
                        test_dirs.append(f"{d.name}/{sub.name}")
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    return test_dirs[:12]


def _find_coverage(issue_text: str, retrieved_files: list,
                   repo_root: Path) -> str:
    if not repo_root.exists():
        return ""

    test_dirs = _find_test_dirs(repo_root)
    if not test_dirs:
        return ""

    terms = _extract_search_terms(issue_text, retrieved_files)
    if not terms:
        return ""

    found_files = []
    deadline = time.monotonic() + TIMEOUT_TOTAL

    for term in terms:
        if time.monotonic() > deadline or len(found_files) >= MAX_TEST_FILES:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(
                ["git", "grep", "-ril", "--fixed-strings", term,
                 "--"] + test_dirs,
                capture_output=True, text=True,
                cwd=str(repo_root),
                timeout=min(remaining, 5.0),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

        if result.returncode != 0 or not result.stdout.strip():
            continue

        for line in result.stdout.strip().splitlines():
            tf = line.strip()
            if tf and tf not in found_files:
                found_files.append(tf)
            if len(found_files) >= MAX_TEST_FILES:
                break

    if not found_files:
        return ""

    lines = ["## Existing Tests\n"]
    for tf in found_files:
        lines.append(f"- `{tf}`")

    result_text = "\n".join(lines)
    return result_text[:MAX_OUTPUT_CHARS]