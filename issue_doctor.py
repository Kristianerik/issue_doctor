#!/usr/bin/env python3
"""
issue_doctor.py — Local AI-powered GitHub issue bug diagnosis tool.

Requires:
  pip install requests rich

Requires Ollama running locally:
  https://ollama.com
  ollama pull deepseek-r1:14b   (recommended)
  ollama pull qwen2.5-coder:14b (alternative)

Usage:
  python issue_doctor.py                        # interactive prompt
  python issue_doctor.py --url <github_url>     # fetch issue from GitHub URL
  python issue_doctor.py --text <file.txt>      # read issue from a text file
  python issue_doctor.py --paste                # paste raw issue text
  python issue_doctor.py --url <url> --skills clang-llvm,concurrency

Configuration via environment variables (optional):
  OLLAMA_HOST     default: http://localhost:11434
  OLLAMA_MODEL    default: deepseek-r1:14b
  GITHUB_TOKEN    for private repos / higher rate limits
  SKILLS_DIR      default: ./skills  (folder next to this script)
"""

import argparse
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

try:
    import requests
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.rule import Rule
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

console = Console()

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-r1:14b")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SKILLS_DIR   = Path(os.environ.get("SKILLS_DIR", Path(__file__).parent / "skills"))

# ── System prompt ─────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """\
You are a principal-level software engineer and compiler/systems expert with
20+ years of debugging experience across large open-source codebases including
LLVM, Linux kernel, GCC, and major web runtimes.

Your task is to produce a precise, actionable bug diagnosis. You must reason
like a senior engineer who has seen this class of bug before:
- Name specific files, functions, and data structures — never vague component names
- Explain the exact mechanism of failure, not just that something "may fail"
- Give investigation steps that are surgical, not exploratory fishing expeditions
- Propose fixes at the code level with real trade-off analysis

STRICT OUTPUT FORMAT — use exactly these markdown sections, in order:

## 1. Issue Summary
One paragraph: what breaks, under what conditions, what the observable symptom is.
Be precise — include the exact function/builtin/flag involved.

## 2. Root Cause Hypothesis
State the most likely root cause as a specific mechanism, e.g.:
"ObjectSizeOffsetEvaluator::visitMember() only resolves counted_by when the
base is a pointer dereference (MemberExpr through IndirectFieldDecl), falling
through to static layout size for direct DeclRefExpr accesses."
Rank alternatives if multiple candidates exist. No vague language.

## 3. Affected Components
Bullet list. For each entry give:
- The exact file path (relative to repo root)
- The specific function or struct involved
- Why it is implicated

## 4. Reproduction Checklist
Numbered, copy-pasteable steps. Include exact compiler flags, environment
setup, and what output to expect vs what you get.

## 5. Investigation Steps
Ordered, surgical steps. Each step must:
- Name the exact tool, flag, or function to inspect
- Say what evidence to look for
- Say what it rules in or out
No generic advice. If you would tell someone to "check the logs", instead say
which log, which field, and what a bad value looks like.

## 6. Proposed Fix
- Identify the exact location to change (file + function + line region if known)
- Show pseudocode or real code for the fix
- Explain why this fix is correct at a mechanical level
- Note any edge cases or regressions the fix could introduce
- If multiple approaches exist, compare them concretely

## 7. Verification Plan
- Specific test cases to add (file location, test framework, what to assert)
- Existing test suite commands to run
- Edge cases that must not regress

## 8. Related Issues / Prior Art
- Name any known bug class this resembles with specifics
- Link to similar past fixes in this codebase if you know of them
- Name anyone who has fixed this class of bug before

Do not hedge with "may", "might", "possibly" unless you explicitly label
it as a lower-confidence hypothesis. Be direct. A developer should be able
to open the right file immediately after reading your diagnosis.
"""

def build_system_prompt(skills: list[str]) -> str:
    """Append any loaded skill content to the base system prompt."""
    if not skills:
        return BASE_SYSTEM_PROMPT

    skill_block = "\n\n---\n\n# Expert Knowledge Base\n\n"
    skill_block += "The following domain-specific knowledge has been loaded for this issue:\n\n"
    skill_block += "\n\n".join(skills)
    return BASE_SYSTEM_PROMPT + skill_block

USER_TEMPLATE = """\
Diagnose the following GitHub issue. Apply every relevant piece of expert
knowledge from the loaded skills. Be specific about file paths and functions.

---
{issue_text}
---
"""

# ── Skills system ─────────────────────────────────────────────────────────────

def load_all_skills() -> dict[str, tuple[list[str], str]]:
    """
    Returns {skill_name: (trigger_keywords, content)} for every skill file
    found in SKILLS_DIR/core/ and SKILLS_DIR/user/.
    """
    skills = {}
    for subdir in ("core", "user"):
        skill_path = SKILLS_DIR / subdir
        if not skill_path.exists():
            continue
        for f in skill_path.glob("*.md"):
            if f.stem == "TEMPLATE":
                continue
            content = f.read_text(encoding="utf-8")
            keywords = _parse_trigger_keywords(content)
            skills[f.stem] = (keywords, content)
    return skills


def _parse_trigger_keywords(content: str) -> list[str]:
    """Extract trigger keywords from a skill file."""
    m = re.search(r"## Trigger keywords\s*\n(.*?)(?:\n##|\Z)", content, re.DOTALL)
    if not m:
        return []
    raw = m.group(1).strip()
    return [kw.strip().lower() for kw in re.split(r"[,\n]+", raw) if kw.strip()]


def auto_detect_skills(issue_text: str, all_skills: dict) -> list[str]:
    """Return skill names whose trigger keywords appear in the issue text."""
    text_lower = issue_text.lower()
    matched = []
    for name, (keywords, _) in all_skills.items():
        if any(kw in text_lower for kw in keywords):
            matched.append(name)
    return matched


def resolve_skills(
    issue_text: str,
    all_skills: dict,
    forced: Optional[list[str]],
    interactive: bool,
) -> list[str]:
    """
    Determine which skills to load:
    1. Auto-detect from issue content
    2. Merge with any --skills overrides
    3. In interactive mode, let the user confirm/adjust
    Returns list of skill content strings ready for injection.
    """
    detected = auto_detect_skills(issue_text, all_skills)

    if forced:
        # merge: forced skills + auto-detected, deduplicated
        final_names = list(dict.fromkeys(forced + detected))
    else:
        final_names = detected

    # Filter to only skills that exist
    valid = {k for k in all_skills}
    final_names = [n for n in final_names if n in valid]

    if interactive:
        available = sorted(all_skills.keys())
        console.print()
        if final_names:
            console.print(f"[bold cyan]Auto-detected skills:[/] {', '.join(final_names)}")
        else:
            console.print("[dim]No skills auto-detected for this issue.[/]")

        if available:
            console.print(f"[dim]Available: {', '.join(available)}[/]")
            override = Prompt.ask(
                "Skills to load (comma-separated, Enter to accept auto-detected, 'none' to skip)",
                default=",".join(final_names) if final_names else "none",
            )
            if override.strip().lower() == "none":
                final_names = []
            else:
                final_names = [s.strip() for s in override.split(",") if s.strip()]
                final_names = [n for n in final_names if n in valid]

    if final_names:
        console.print(f"[green]Loading skills:[/] {', '.join(final_names)}")

    return [all_skills[n][1] for n in final_names if n in all_skills]


# ── GitHub fetching ───────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str, int]:
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    m = re.search(pattern, url)
    if not m:
        raise ValueError(f"Cannot parse GitHub issue URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_github_issue(url: str) -> str:
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

    short = OLLAMA_MODEL.split(":")[0]
    if not any(short in m for m in models):
        console.print(
            f"[yellow]Model [bold]{OLLAMA_MODEL}[/] not found locally.[/]\n"
            f"Pull it with:  [cyan]ollama pull {OLLAMA_MODEL}[/]\n"
            f"Available: {', '.join(models) or 'none'}"
        )
        sys.exit(1)


def stream_diagnosis(issue_text: str, system_prompt: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": USER_TEMPLATE.format(issue_text=issue_text)},
        ],
        "stream": True,
        "options": {
            "temperature": 0.15,
            "num_predict": 6144,
            "num_ctx": 16384,
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
    console.print("[dim]Paste the issue text, then press Ctrl-D (Mac/Linux) or Ctrl-Z + Enter (Windows):[/]")
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

def print_banner(skill_count: int) -> None:
    skills_info = f"{skill_count} skills available" if skill_count else "no skills found (add some to ./skills/)"
    console.print(Panel.fit(
        f"[bold cyan]issue_doctor[/]  [dim]— local AI bug diagnosis[/]\n"
        f"[dim]model: {OLLAMA_MODEL}   host: {OLLAMA_HOST}   {skills_info}[/]",
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


def save_report(diagnosis: str, issue_text: str, loaded_skill_names: list[str]) -> None:
    save = Prompt.ask("\nSave diagnosis to file?", choices=["y", "n"], default="n")
    if save == "y":
        default_name = f"diagnosis_{int(time.time())}.md"
        filename = Prompt.ask("Filename", default=default_name)
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# Issue Doctor Report\n\n")
            if loaded_skill_names:
                f.write(f"**Skills loaded:** {', '.join(loaded_skill_names)}\n\n")
            f.write("## Original Issue\n\n```\n")
            f.write(issue_text[:3000])
            f.write("\n```\n\n## Diagnosis\n\n")
            f.write(diagnosis)
        console.print(f"[green]Saved to [bold]{filename}[/][/]")


def main() -> None:
    global OLLAMA_MODEL, OLLAMA_HOST

    parser = argparse.ArgumentParser(
        description="Diagnose GitHub issues using a local Ollama model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              OLLAMA_HOST    Ollama server URL  (default: http://localhost:11434)
              OLLAMA_MODEL   Model to use       (default: deepseek-r1:14b)
              GITHUB_TOKEN   GitHub PAT         (optional, for private repos)
              SKILLS_DIR     Path to skills dir (default: ./skills)
        """),
    )
    parser.add_argument("--url",    metavar="URL",    help="GitHub issue URL to fetch and diagnose")
    parser.add_argument("--text",   metavar="FILE",   help="Path to a text file containing the issue")
    parser.add_argument("--paste",  action="store_true", help="Paste raw issue text via stdin")
    parser.add_argument("--model",  metavar="NAME",   help=f"Override model (default: {OLLAMA_MODEL})")
    parser.add_argument("--host",   metavar="URL",    help=f"Override Ollama host (default: {OLLAMA_HOST})")
    parser.add_argument("--skills", metavar="NAMES",  help="Comma-separated skill names to force-load (e.g. clang-llvm,concurrency)")
    parser.add_argument("--no-skills", action="store_true", help="Disable all skill loading")
    args = parser.parse_args()

    if args.model:
        OLLAMA_MODEL = args.model
    if args.host:
        OLLAMA_HOST = args.host

    all_skills = {} if args.no_skills else load_all_skills()
    print_banner(len(all_skills))
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

    forced_skills = [s.strip() for s in args.skills.split(",")] if args.skills else None
    is_interactive = not (args.url or args.text or args.paste)

    loaded_skill_contents = resolve_skills(
        issue_text, all_skills, forced_skills, interactive=is_interactive
    )
    loaded_skill_names = [
        name for name, (_, content) in all_skills.items()
        if content in loaded_skill_contents
    ]

    system_prompt = build_system_prompt(loaded_skill_contents)
    diagnosis = stream_diagnosis(issue_text, system_prompt)

    save_report(diagnosis, issue_text, loaded_skill_names)

    console.print()
    console.print(Rule("[dim]Done[/]"))


if __name__ == "__main__":
    main()