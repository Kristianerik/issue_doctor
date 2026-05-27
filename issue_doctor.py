#!/usr/bin/env python3
"""
issue_doctor.py — Local AI-powered GitHub issue bug diagnosis tool.

Requires:
  pip install requests rich

Requires Ollama running locally:
  https://ollama.com
  ollama pull deepseek-r1:14b   (or qwen2.5-coder:14b)

Usage:
  python issue_doctor.py                        # interactive prompt
  python issue_doctor.py --url <github_url>     # fetch issue from GitHub URL
  python issue_doctor.py --text <file.txt>      # read issue from a text file
  python issue_doctor.py --paste                # paste raw issue text

Configuration via environment variables (optional):
  OLLAMA_HOST   — default: http://localhost:11434
  OLLAMA_MODEL  — default: deepseek-r1:14b
  GITHUB_TOKEN  — for private repos / higher rate limits
"""

import argparse
import json
import os
import re
import sys
import textwrap
import time
from typing import Optional

try:
    import requests
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.rule import Rule
    from rich.spinner import Spinner
    from rich.live import Live
    from rich.text import Text
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

console = Console()

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-r1:14b")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ── Prompts ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior software engineer and debugging expert. Your job is to
diagnose GitHub issues using structured, methodical reasoning.

When given a bug report you will always produce a diagnosis structured in
exactly these sections, using markdown headers:

## 1. Issue Summary
One-paragraph plain-English restatement of what is broken and when.

## 2. Root Cause Hypothesis
The most likely technical root cause, explained clearly. If multiple
candidates exist, rank them.

## 3. Affected Components
Bullet list of files, modules, subsystems, or code paths likely involved.

## 4. Reproduction Checklist
Numbered steps a developer can follow to reproduce the issue locally.

## 5. Investigation Steps
Ordered list of concrete debugging actions (log points, assertions,
bisect strategies, test cases) to confirm the root cause.

## 6. Proposed Fix
Specific, actionable fix with pseudocode or real code snippets where
possible. If multiple approaches exist, compare trade-offs.

## 7. Verification Plan
How to confirm the fix is correct: unit tests, integration tests,
edge cases to exercise, regression checks.

## 8. Related Issues / Prior Art
Any patterns, known bugs, or common mistakes this resembles. If you
recognise a known bug class (e.g. scalable-vector type confusion in LLVM
loop vectorisers) say so.

Be precise. Avoid vague advice like "check the logs". Give the developer
something they can act on immediately.
"""

USER_TEMPLATE = """\
Please diagnose the following GitHub issue:

---
{issue_text}
---
"""

# ── GitHub fetching ───────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str, int]:
    """Return (owner, repo, issue_number) from a GitHub issue URL."""
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    m = re.search(pattern, url)
    if not m:
        raise ValueError(f"Cannot parse GitHub issue URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_github_issue(url: str) -> str:
    """Fetch issue title, body, and top comments from the GitHub API."""
    owner, repo, number = parse_github_url(url)
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"

    with console.status("[bold cyan]Fetching issue from GitHub…"):
        resp = requests.get(api, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        comments_resp = requests.get(data["comments_url"], headers=headers, timeout=15)
        comments_resp.raise_for_status()
        comments = comments_resp.json()

    parts = [
        f"Repository: {owner}/{repo}",
        f"Issue #{number}: {data['title']}",
        f"State: {data['state']}",
        f"Labels: {', '.join(l['name'] for l in data.get('labels', [])) or 'none'}",
        "",
        "## Issue Body",
        data.get("body") or "(no body)",
    ]

    if comments:
        parts.append("\n## Comments (up to 5)")
        for c in comments[:5]:
            parts.append(f"\n**@{c['user']['login']}:**\n{c['body']}")

    return "\n".join(parts)


# ── Ollama interaction ────────────────────────────────────────────────────────

def check_ollama() -> None:
    """Verify Ollama is reachable and the model is available."""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
    except requests.exceptions.ConnectionError:
        console.print(
            f"[bold red]Cannot reach Ollama at {OLLAMA_HOST}[/]\n"
            "Make sure Ollama is running:  [cyan]ollama serve[/]"
        )
        sys.exit(1)

    # Normalise: "deepseek-r1:14b" matches "deepseek-r1:14b" or stored variants
    short = OLLAMA_MODEL.split(":")[0]
    if not any(short in m for m in models):
        console.print(
            f"[yellow]Model [bold]{OLLAMA_MODEL}[/] not found locally.[/]\n"
            f"Pull it with:  [cyan]ollama pull {OLLAMA_MODEL}[/]\n"
            f"Available: {', '.join(models) or 'none'}"
        )
        sys.exit(1)


def stream_diagnosis(issue_text: str) -> str:
    """Stream the model's diagnosis, rendering markdown as it arrives."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": USER_TEMPLATE.format(issue_text=issue_text)},
        ],
        "stream": True,
        "options": {
            "temperature": 0.2,   # low temp for analytical reasoning
            "num_predict": 4096,
        },
    }

    url = f"{OLLAMA_HOST}/api/chat"
    full_response = []

    console.print()
    console.print(Rule(f"[bold cyan]Diagnosis · {OLLAMA_MODEL}"))
    console.print()

    with requests.post(url, json=payload, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        buffer = ""
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            token = chunk.get("message", {}).get("content", "")
            if token:
                full_response.append(token)
                buffer += token
                # Flush on newlines so markdown renders progressively
                if "\n" in buffer:
                    lines = buffer.split("\n")
                    for line in lines[:-1]:
                        console.print(line)
                    buffer = lines[-1]

            if chunk.get("done"):
                if buffer:
                    console.print(buffer)
                break

    return "".join(full_response)


# ── Input helpers ─────────────────────────────────────────────────────────────

def read_paste() -> str:
    """Read multi-line paste from stdin until the user sends EOF (Ctrl-D/Z)."""
    console.print("[dim]Paste the issue text below, then press Ctrl-D (Mac/Linux) or Ctrl-Z + Enter (Windows):[/]")
    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass
    return "\n".join(lines)


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── Main ──────────────────────────────────────────────────────────────────────

def print_banner() -> None:
    console.print(Panel.fit(
        "[bold cyan]issue_doctor[/]  [dim]— local AI bug diagnosis[/]\n"
        f"[dim]model: {OLLAMA_MODEL}   host: {OLLAMA_HOST}[/]",
        border_style="cyan",
    ))
    console.print()


def get_issue_text(args: argparse.Namespace) -> str:
    if args.url:
        return fetch_github_issue(args.url)

    if args.text:
        return read_file(args.text)

    if args.paste:
        return read_paste()

    # Interactive mode: ask the user
    console.print("How would you like to provide the issue?")
    console.print("  [bold]1[/] — GitHub URL")
    console.print("  [bold]2[/] — Paste raw text")
    console.print("  [bold]3[/] — Read from file")
    choice = Prompt.ask("\nChoice", choices=["1", "2", "3"], default="1")

    if choice == "1":
        url = Prompt.ask("GitHub issue URL")
        return fetch_github_issue(url)
    elif choice == "2":
        return read_paste()
    else:
        path = Prompt.ask("File path")
        return read_file(path)


def save_report(diagnosis: str, issue_text: str) -> None:
    """Optionally save the diagnosis to a markdown file."""
    save = Prompt.ask("\nSave diagnosis to file?", choices=["y", "n"], default="n")
    if save == "y":
        default_name = f"diagnosis_{int(time.time())}.md"
        filename = Prompt.ask("Filename", default=default_name)
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# Issue Doctor Report\n\n")
            f.write("## Original Issue\n\n")
            f.write("```\n")
            f.write(issue_text[:3000])
            f.write("\n```\n\n")
            f.write("## Diagnosis\n\n")
            f.write(diagnosis)
        console.print(f"[green]Saved to [bold]{filename}[/][/]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose GitHub issues using a local Ollama model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              OLLAMA_HOST    Ollama server URL  (default: http://localhost:11434)
              OLLAMA_MODEL   Model to use       (default: deepseek-r1:14b)
              GITHUB_TOKEN   GitHub PAT         (optional, for private repos)
        """),
    )
    parser.add_argument("--url",   metavar="URL",  help="GitHub issue URL to fetch and diagnose")
    parser.add_argument("--text",  metavar="FILE", help="Path to a text file containing the issue")
    parser.add_argument("--paste", action="store_true", help="Paste raw issue text via stdin")
    parser.add_argument("--model", metavar="NAME", help=f"Override model (default: {OLLAMA_MODEL})")
    parser.add_argument("--host",  metavar="URL",  help=f"Override Ollama host (default: {OLLAMA_HOST})")
    args = parser.parse_args()

    global OLLAMA_MODEL, OLLAMA_HOST
    if args.model:
        OLLAMA_MODEL = args.model
    if args.host:
        OLLAMA_HOST = args.host

    print_banner()
    check_ollama()

    issue_text = get_issue_text(args)
    if not issue_text.strip():
        console.print("[red]No issue text provided. Exiting.[/]")
        sys.exit(1)

    console.print()
    console.print(Panel(
        issue_text[:600] + ("…" if len(issue_text) > 600 else ""),
        title="[bold]Issue (preview)[/]",
        border_style="dim",
    ))

    diagnosis = stream_diagnosis(issue_text)

    save_report(diagnosis, issue_text)

    console.print()
    console.print(Rule("[dim]Done[/]"))


if __name__ == "__main__":
    main()
