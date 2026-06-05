"""
diagnosis.py - LLM streaming, confidence validation, and report saving.

Depends on: config.py, prompt.py
"""

import json
import re
import sys
import time
from pathlib import Path

import requests

import config
from config import OLLAMA_HOST, OLLAMA_MODEL
from prompt import USER_TEMPLATE

try:
    from rich.prompt import Prompt
    from rich.rule import Rule
except ImportError:
    class Prompt:
        @staticmethod
        def ask(msg, **kw): return input(msg + ": ")
    class Rule:
        def __init__(self, *a, **kw): pass
from config import console


# ── Ollama health check ────────────────────────────────────────────────────────

def check_ollama():
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Cannot reach Ollama at {OLLAMA_HOST}[/]")
        sys.exit(1)
    short = OLLAMA_MODEL.split(":")[0]
    if not any(short in m for m in models):
        console.print(f"[yellow]Model {OLLAMA_MODEL} not found.[/]")
        console.print(f"Pull it:  ollama pull {OLLAMA_MODEL}")
        sys.exit(1)


# ── Streaming diagnosis ────────────────────────────────────────────────────────

def stream_diagnosis(issue_text, system_prompt):
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": USER_TEMPLATE.format(issue_text=issue_text)},
        ],
        "stream": True,
        "options": {"temperature": 0.15, "num_predict": 4096, "num_ctx": 16384},
    }
    console.print()
    console.print(Rule(f"[bold cyan]Diagnosis + Draft Patch  {OLLAMA_MODEL}"))
    console.print()
    full_response = []
    with requests.post(f"{OLLAMA_HOST}/api/chat", json=payload,
                       stream=True, timeout=600) as resp:
        if not resp.ok:
            console.print(f"[red]Ollama error {resp.status_code}: {resp.text[:500]}[/]")
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


# ── Confidence validation ──────────────────────────────────────────────────────

def validate_confidence(diagnosis, repo_context):
    """Post-generation external confidence check — appends warning if files are unverified."""
    if not repo_context:
        return diagnosis

    cited_files = set()
    for pat in [
        r'`([^`\n]{3,80}\.[ch](?:pp?)?)`',
        r'--- a/([^\n]+\.[ch](?:pp?)?)',
        r'File[:\s*]+([^\n`\s]+\.[ch](?:pp?)?)',
    ]:
        cited_files.update(re.findall(pat, diagnosis))

    if not cited_files:
        return diagnosis

    retrieved_files = set(re.findall(r'### ([^\n]+\.[ch](?:pp?)?)', repo_context))

    hallucinated = []
    for cited in cited_files:
        cited_name = cited.strip().split('/')[-1]
        if not any(cited_name in rf for rf in retrieved_files):
            close = [rf for rf in retrieved_files if cited_name[:12].lower() in rf.lower()]
            hallucinated.append((cited, close))

    if not hallucinated:
        return diagnosis

    lines = [
        '', '---', '## Automated Retrieval Warning', '',
        'The following files cited in this diagnosis were **not present** '
        'in the retrieved source chunks. They may be correct (cited from '
        'model training knowledge) or hallucinated:', '',
    ]
    for cited, close in hallucinated:
        lines.append(f'- `{cited}` — NOT IN RETRIEVED CHUNKS')
        if close:
            close_str = ', '.join(f'`{c}`' for c in close[:3])
            lines.append(f'  - Similar retrieved files: {close_str}')
    lines += [
        '', '**Recommended action:**',
        '- Verify all cited files exist in your local repo before applying',
        '- If a similar file was retrieved above, check whether the cited',
        '  file is a variant or correct',
        '- Treat unverified citations as direction only, not confirmed fixes',
    ]
    return diagnosis + '\n' + '\n'.join(lines)


# ── Input helpers ──────────────────────────────────────────────────────────────

def read_paste():
    console.print("[dim]Paste issue text, then Ctrl-D (Mac/Linux) or Ctrl-Z+Enter (Windows):[/]")
    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass
    return "\n".join(lines)


def get_issue_text(args):
    from query import fetch_github_issue
    if args.url:
        return fetch_github_issue(args.url)
    if args.text:
        return open(args.text, encoding="utf-8").read()
    if args.paste:
        return read_paste()
    console.print("  [bold]1[/] GitHub URL  [bold]2[/] Paste  [bold]3[/] File")
    choice = Prompt.ask("Choice", choices=["1","2","3"], default="1")
    if choice == "1":
        return fetch_github_issue(Prompt.ask("GitHub issue URL"))
    elif choice == "2":
        return read_paste()
    else:
        return open(Prompt.ask("File path"), encoding="utf-8").read()


# ── Report saving ──────────────────────────────────────────────────────────────

def save_report(diagnosis, issue_text, skill_names, used_rag):
    if Prompt.ask("\nSave report?", choices=["y","n"], default="n") == "y":
        filename = Prompt.ask("Filename", default=f"diagnosis_{int(time.time())}.md")
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# Issue Doctor Report\n\n")
            f.write(f"**Skills:** {', '.join(skill_names) or 'none'}\n")
            f.write(f"**Retrieval:** {'RAG (semantic)' if used_rag else 'keyword scan'}\n\n")
            f.write("## Original Issue\n\n```\n")
            f.write(issue_text[:3000])
            f.write("\n```\n\n## Diagnosis + Draft Patch\n\n")
            f.write(diagnosis)
        console.print(f"[green]Saved to [bold]{filename}[/][/]")