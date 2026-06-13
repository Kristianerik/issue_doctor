"""
agents/godbolt.py - Fetch reproducer source code from Godbolt short URLs.

Extracts Godbolt short URLs from issue text, fetches the source code via
the Godbolt API, and returns it as a ## Reproducer section.

Design constraints:
- Hard 10s timeout
- Fails silently — returns empty string on any error
- Stateless, read-only, no LLM calls
- Works for any Godbolt link in any issue
"""

import re

import requests


MAX_SOURCE_CHARS = 1500
GODBOLT_API = "https://godbolt.org/api/shortlinkinfo/{id}"


def get_godbolt_context(issue_text: str) -> str:
    """
    Extract Godbolt short URL from issue text, fetch source, return as
    ## Reproducer section. Returns empty string on any error.
    """
    try:
        return _fetch_godbolt(issue_text)
    except Exception:
        return ""


def _fetch_godbolt(issue_text: str) -> str:
    # Extract Godbolt short link ID
    m = re.search(r'godbolt\.org/z/([A-Za-z0-9]+)', issue_text)
    if not m:
        return ""

    link_id = m.group(1)

    r = requests.get(
        GODBOLT_API.format(id=link_id),
        timeout=10,
        headers={"Accept": "application/json"},
    )
    if r.status_code != 200:
        return ""

    data = r.json()

    # Extract source from sessions[0].source
    sessions = data.get("sessions", [])
    if not sessions:
        return ""

    source = sessions[0].get("source", "").strip()
    if not source:
        return ""

    # Detect language for syntax highlighting
    lang = "cpp"
    compilers = sessions[0].get("compilers", [])
    if compilers:
        compiler_id = compilers[0].get("id", "")
        if "python" in compiler_id.lower():
            lang = "python"
        elif "rust" in compiler_id.lower():
            lang = "rust"

    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS] + "\n// ...(truncated)"

    return f"## Reproducer (from Godbolt)\n```{lang}\n{source}\n```\n"
