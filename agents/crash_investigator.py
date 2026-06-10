"""
agents/crash_investigator.py - Bounded agentic crash investigation.

Runs before the main diagnosis for crash issues. Two steps:

Step 1 (deterministic): Extract the crash frame from the symbolized stacktrace,
grep the repo for that function's source.

Step 2 (LLM-guided): Make a single focused Ollama call with the stacktrace +
crash frame source. Ask which frame is most likely the fix location. Grep
that function's source.

Returns a string of source context capped at ~800 tokens, labeled as
## Crash Investigation. Fails silently on any error or timeout.

Design constraints:
- Hard 30s timeout on the LLM call
- Max 150 tokens LLM output
- Fails silently — never blocks the main diagnosis
- Stateless — reads from repo, writes nothing
"""

import re
import subprocess
from pathlib import Path
from typing import Optional

import requests

import config
from config import console


# ── Token budget ──────────────────────────────────────────────────────────────

MAX_SOURCE_CHARS = 3200  # ~800 tokens at 4 chars/token


# ── Step 1: Deterministic stacktrace extraction ───────────────────────────────

def extract_crash_frame_function(issue_text: str) -> Optional[str]:
    """
    Extract the function name from the first symbolized frame at or after
    frame 4 in the stacktrace. Frames 0-3 are signal handlers — skip them.

    Returns the bare function name (no namespace, no args) or None.
    """
    # Match lines like: #9  0x... FunctionName(args) File.cpp:line
    # or: #9  0x... namespace::ClassName::FunctionName(args)
    frame_pattern = re.compile(
        r'#(\d+)\s+0x[0-9a-f]+\s+'
        r'(?:\(anonymous namespace\)::)?'
        r'([\w:~<>]+(?:::\w+)*)\s*\(',
        re.IGNORECASE
    )
    for m in frame_pattern.finditer(issue_text):
        frame_num = int(m.group(1))
        if frame_num < 4:
            continue
        full_name = m.group(2).strip()
        # Take the last component of the qualified name
        bare = full_name.split('::')[-1].strip()
        if len(bare) >= 4 and bare not in {
            'abort', 'raise', 'kill', 'gsignal', 'signal',
            'pthread_kill', 'PrintStackTrace', 'CleanupOnSignal',
        }:
            return bare
    return None


def grep_function_source(function_name: str, repo_root: Path,
                          max_chars: int = MAX_SOURCE_CHARS) -> str:
    """
    Grep the repo for the function definition and return surrounding source.
    Uses git grep for speed on large repos.
    Returns empty string if not found or on error.
    """
    if not function_name or not repo_root.exists():
        return ""

    # Single grep pass: require whitespace before function name to avoid
    # matching calls inside expressions. No --word-regexp (breaks on '(').
    EXCLUDE_PATHSPECS = [
        ":(exclude)build",
        ":(exclude)*/build",
        ":(exclude)*/test*",
        ":(exclude)*/unittests",
        ":(exclude)docs",
        ":(exclude)doc",
        ":(exclude)cmake",
        ":(exclude)benchmarks",
        ":(exclude)third-party",
        ":(exclude)third_party",
        ":(exclude)external",
        ":(exclude)vendor",
    ]

    pat = f"[[:space:]]{function_name}("

    for _ in [1]:  # single-pass, keep for/try structure
        try:
            result = subprocess.run(
                ["git", "grep", "-n", pat,
                 "--"] + EXCLUDE_PATHSPECS + ["."],
                capture_output=True, text=True,
                cwd=str(repo_root), timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                break

            # Find first match in a source file
            def_line = None
            match_file = None
            for line in result.stdout.splitlines():
                m = re.match(r'([^:]+\.(?:cpp|cc|c|h|hpp)):(\d+):', line)
                if m:
                    match_file = m.group(1)
                    def_line = int(m.group(2))
                    break

            if def_line is None or match_file is None:
                break

            # Read 60 lines of context around the definition
            filepath = repo_root / match_file
            try:
                source_lines = filepath.read_text(
                    encoding='utf-8', errors='ignore'
                ).splitlines()
            except OSError:
                break

            start = max(0, def_line - 3)
            end = min(len(source_lines), def_line + 57)
            snippet = "\n".join(
                f"{start+i+1:4d}  {line}"
                for i, line in enumerate(source_lines[start:end])
            )
            label = f"// {match_file} (lines {start+1}-{end})\n"
            return (label + snippet)[:max_chars]

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            break

    return ""


# ── Step 2: LLM-guided fix location identification ────────────────────────────

def ask_fix_location(stacktrace: str, crash_source: str,
                     crash_fn: str) -> Optional[str]:
    """
    Make a single focused Ollama call: given the stacktrace and the crash
    frame's source, which function is most likely the fix location?

    Returns a function name or None on failure/timeout.
    """
    # Truncate inputs to keep the prompt small
    st_truncated = stacktrace[:1500]
    src_truncated = crash_source[:1200]

    prompt = f"""Stacktrace (crash is in frame 4+):
{st_truncated}

Source of crash function `{crash_fn}`:
{src_truncated}

The crash is in `{crash_fn}`. The FIX goes in its CALLER — the function that
passed invalid data to `{crash_fn}`.

Looking at the stacktrace frames above `{crash_fn}`, which single function name
is most likely where the fix should go? Reply with ONLY the bare function name,
nothing else. No explanation, no namespace prefix, just the function name."""

    try:
        r = requests.post(
            f"{config.OLLAMA_HOST}/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.0, "num_predict": 150},
            },
            timeout=30,
        )
        if r.status_code != 200:
            return None
        raw = r.json().get("message", {}).get("content", "").strip()
        # Strip thinking tags if present
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        # Extract just the function name (first word-like token)
        m = re.search(r'\b([A-Za-z_]\w{3,})\b', raw)
        return m.group(1) if m else None
    except Exception:
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

def investigate_crash(issue_text: str, repo_root: Path) -> str:
    """
    Run the two-step crash investigation.

    Step 1: Extract crash frame function, grep its source (deterministic).
    Step 2: Ask LLM which frame is the fix location, grep that source (agentic).

    Returns a string labeled ## Crash Investigation, capped at ~800 tokens.
    Fails silently — returns empty string on any error.
    """
    try:
        return _investigate(issue_text, repo_root)
    except Exception as e:
        console.print(f"[dim]  Crash investigator failed silently: {e}[/]")
        return ""


def _investigate(issue_text: str, repo_root: Path) -> str:
    parts = []

    # ── Step 1: Deterministic crash frame extraction ──────────────────────────
    crash_fn = extract_crash_frame_function(issue_text)
    if not crash_fn:
        return ""

    console.print(f"[dim]  Crash investigator: crash function = {crash_fn}[/]")
    crash_source = grep_function_source(crash_fn, repo_root,
                                        max_chars=MAX_SOURCE_CHARS // 2)
    if crash_source:
        parts.append(f"### Crash site: `{crash_fn}`\n```cpp\n{crash_source}\n```")

    # ── Step 2: LLM-guided fix location ──────────────────────────────────────
    # Extract the raw stacktrace block for the LLM prompt
    stacktrace_match = re.search(
        r'((?:#\d+\s+0x[0-9a-f]+.*\n?){3,})', issue_text
    )
    stacktrace = stacktrace_match.group(1) if stacktrace_match else issue_text[:800]

    fix_fn = ask_fix_location(stacktrace, crash_source, crash_fn)
    if fix_fn and fix_fn.lower() != crash_fn.lower():
        console.print(f"[dim]  Crash investigator: suggested fix location = {fix_fn}[/]")
        fix_source = grep_function_source(fix_fn, repo_root,
                                          max_chars=MAX_SOURCE_CHARS // 2)
        if fix_source:
            parts.append(
                f"### Suggested fix location: `{fix_fn}`\n"
                f"```cpp\n{fix_source}\n```"
            )
    elif fix_fn:
        console.print(f"[dim]  Crash investigator: LLM suggested same frame ({fix_fn})[/]")

    if not parts:
        return ""

    result = "## Crash Investigation\n\n" + "\n\n".join(parts)
    return result[:MAX_SOURCE_CHARS + 200]  # small header overhead