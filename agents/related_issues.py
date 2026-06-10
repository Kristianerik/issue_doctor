"""
agents/related_issues.py - Related issue search via GitHub API.

Given issue text and retrieved file paths, searches GitHub for closed issues
in the same repo that mention the same function names. Returns a compact
## Related Issues summary capped at 400 chars (~100 tokens).

Design constraints:
- Hard 10s timeout
- Fails silently if no token, network error, or no results
- Works on any GitHub repo, not LLVM-specific
- Read-only, stateless
- Graceful fallback if GITHUB_TOKEN is empty
"""

import re
from typing import Optional

import requests

import config


MAX_OUTPUT_CHARS = 400


def get_related_issues(
    issue_text: str,
    retrieved_files: list,
    repo_owner: str,
    repo_name: str,
    github_token: Optional[str] = None,
) -> str:
    """
    Search GitHub for closed issues mentioning the same functions/files.
    Returns ## Related Issues summary or empty string.
    """
    try:
        return _search_issues(
            issue_text, retrieved_files, repo_owner, repo_name, github_token
        )
    except Exception:
        return ""


def _extract_search_terms(issue_text: str, retrieved_files: list) -> list:
    """
    Extract 2-3 high-signal search terms from the issue text.
    Prefer function names from stacktrace, fall back to retrieved file basenames.
    """
    terms = []

    # Extract function names from stacktrace frames 4+
    frame_pattern = re.compile(
        r'#(\d+)\s+0x[0-9a-f]+\s+'
        r'(?:\(anonymous namespace\)::)?'
        r'([\w:~]+(?:::\w+)*)\s*\(',
    )
    for m in frame_pattern.finditer(issue_text):
        if int(m.group(1)) < 4:
            continue
        bare = m.group(2).split('::')[-1].strip()
        if (len(bare) >= 6
                and bare not in {'abort', 'raise', 'kill', 'gsignal',
                                  'pthread_kill', 'PrintStackTrace'}):
            terms.append(bare)
            if len(terms) >= 2:
                break

    # Extract function names from backtick identifiers in issue text
    if len(terms) < 2:
        for m in re.finditer(r'`([A-Za-z_]\w{5,})\(\)`', issue_text):
            fn = m.group(1)
            if fn not in terms:
                terms.append(fn)
                if len(terms) >= 2:
                    break

    # Fall back to retrieved file basenames
    if not terms:
        for filepath in retrieved_files[:2]:
            basename = filepath.split('/')[-1].replace('.cpp', '').replace('.c', '')
            if len(basename) >= 6:
                terms.append(basename)

    return terms[:3]


def _search_issues(
    issue_text: str,
    retrieved_files: list,
    repo_owner: str,
    repo_name: str,
    github_token: Optional[str],
) -> str:
    token = github_token or config.GITHUB_TOKEN
    if not token:
        return ""  # Skip silently — unauthenticated search is heavily rate-limited

    terms = _extract_search_terms(issue_text, retrieved_files)
    if not terms:
        return ""

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    found = []
    for term in terms:
        if len(found) >= 3:
            break
        query = f"{term} repo:{repo_owner}/{repo_name} is:closed is:issue"
        try:
            r = requests.get(
                "https://api.github.com/search/issues",
                headers=headers,
                params={"q": query, "per_page": 3, "sort": "updated"},
                timeout=10,
            )
            if r.status_code == 403:
                return ""  # Rate limited — fail silently
            if r.status_code != 200:
                continue
            items = r.json().get("items", [])
            for item in items:
                number = item.get("number")
                title = item.get("title", "")[:60]
                url = item.get("html_url", "")
                entry = (number, title, url)
                if entry not in found:
                    found.append(entry)
                if len(found) >= 3:
                    break
        except Exception:
            continue

    if not found:
        return ""

    lines = ["## Related Issues\n"]
    for number, title, url in found:
        line = f"- #{number}: {title} — {url}"
        lines.append(line)

    result = "\n".join(lines)
    return result[:MAX_OUTPUT_CHARS]
