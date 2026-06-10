#!/usr/bin/env python3
"""
issue_doctor.py - Local AI-powered GitHub issue bug diagnosis + draft patch.

Requires:
  pip install requests rich sqlite-vec

Requires Ollama:
  ollama pull qwen3:14b
  ollama pull nomic-embed-text

Usage:
  python issue_doctor.py --url <github_url>
  python issue_doctor.py --url <url> --repo /path/to/repo
  python issue_doctor.py --url <url> --reindex
  python issue_doctor.py --diagnose
"""

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import requests
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich sqlite-vec")
    sys.exit(1)

try:
    import sqlite_vec
    SQLITE_VEC_AVAILABLE = True
except ImportError:
    SQLITE_VEC_AVAILABLE = False

import config
from diagnosis import check_ollama, get_commit_history_context, get_issue_text, get_missing_cited_files, get_related_issues_context, investigate_crash_issue, save_report, stream_diagnosis, validate_confidence
from prompt import build_system_prompt, get_skill_keywords, load_all_skills, resolve_skills
from query import resolve_repo_context
from rag import check_embed_model_availability, embed_files_on_demand, hybrid_retrieve, format_retrieved_chunks

console = Console()


def print_banner(skill_count, rag_available):
    rag_status = "RAG ready" if rag_available else "RAG unavailable (keyword fallback)"
    console.print(Panel.fit(
        f"[bold cyan]issue_doctor[/]  [dim]local AI bug diagnosis + draft patch[/]\n"
        f"[dim]model: {config.OLLAMA_MODEL}   embed: {config.EMBED_MODEL}   "
        f"skills: {skill_count}   {rag_status}[/]",
        border_style="cyan",
    ))
    console.print()


def main():
    parser = argparse.ArgumentParser(description="Local AI bug diagnosis + draft patch.")
    parser.add_argument("--url",       metavar="URL")
    parser.add_argument("--text",      metavar="FILE")
    parser.add_argument("--paste",     action="store_true")
    parser.add_argument("--model",     metavar="NAME")
    parser.add_argument("--host",      metavar="URL")
    parser.add_argument("--embed",     metavar="NAME")
    parser.add_argument("--skills",    metavar="NAMES")
    parser.add_argument("--no-skills", action="store_true")
    parser.add_argument("--repo",      metavar="PATH")
    parser.add_argument("--no-repo",   action="store_true")
    parser.add_argument("--reindex",   action="store_true")
    parser.add_argument("--update",    action="store_true",
                        help="Incrementally update index with changed files only")
    parser.add_argument("--max-files", type=int, default=3000, metavar="N")
    parser.add_argument("--diagnose",  action="store_true")
    parser.add_argument("--no-save",   action="store_true",
                        help="Skip save prompt (for scripted use)")
    parser.add_argument("--save-to",   metavar="FILE",
                        help="Save diagnosis to this file directly")
    args = parser.parse_args()

    if args.model:  config.OLLAMA_MODEL = args.model
    if args.host:   config.OLLAMA_HOST  = args.host
    if args.embed:  config.EMBED_MODEL  = args.embed

    if args.diagnose:
        console.print("[bold cyan]issue_doctor diagnostics[/]")
        console.print(f"  OLLAMA_HOST:  {config.OLLAMA_HOST}")
        console.print(f"  OLLAMA_MODEL: {config.OLLAMA_MODEL}")
        console.print(f"  EMBED_MODEL:  {config.EMBED_MODEL}")
        console.print(f"  SKILLS_DIR:   {config.SKILLS_DIR}")
        console.print(f"  sqlite-vec:   {'available' if SQLITE_VEC_AVAILABLE else 'NOT available'}")
        try:
            r = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=3)
            models = [m["name"] for m in r.json().get("models", [])]
            console.print(f"  Ollama:       reachable")
            console.print(f"  Models:       {', '.join(models) or 'none'}")
            embed_short = config.EMBED_MODEL.split(":")[0].lower()
            model_short = config.OLLAMA_MODEL.split(":")[0].lower()
            console.print(f"  Embed model:  {'FOUND' if any(embed_short in m.lower() for m in models) else 'NOT FOUND'}")
            console.print(f"  Diag model:   {'FOUND' if any(model_short in m.lower() for m in models) else 'NOT FOUND'}")
        except Exception as e:
            console.print(f"  Ollama:       ERROR - {e}")
        sys.exit(0)

    all_skills = {} if args.no_skills else load_all_skills()
    rag_available = SQLITE_VEC_AVAILABLE and check_embed_model_availability()
    print_banner(len(all_skills), rag_available)
    check_ollama()

    issue_text = get_issue_text(args)
    if not issue_text.strip():
        console.print("[red]No issue text provided.[/]")
        sys.exit(1)

    console.print()
    console.print(Panel(
        issue_text[:600] + ("..." if len(issue_text) > 600 else ""),
        title="[bold]Issue (preview)[/]", border_style="dim",
    ))

    forced = [s.strip() for s in args.skills.split(",")] if args.skills else None
    is_interactive = not (args.url or args.text or args.paste)
    repo_root_path = args.repo if args.repo else None
    skill_contents, skill_names = resolve_skills(
        issue_text, all_skills, forced, is_interactive, repo_root=repo_root_path)
    skill_keywords = get_skill_keywords(all_skills, skill_names)

    repo_context, used_rag, rag_conn, repo_root, retrieved_files = resolve_repo_context(
        args, issue_text, skill_contents, skill_keywords)

    # Run crash investigator after check_ollama so qwen3:14b is already loaded
    crash_context = investigate_crash_issue(issue_text, repo_root)
    commit_context = get_commit_history_context(retrieved_files, repo_root)
    related_context = get_related_issues_context(
        issue_text, retrieved_files, args.url or "")

    system_prompt = build_system_prompt(
        skill_contents, repo_context, used_rag,
        issue_text=issue_text, crash_context=crash_context,
        commit_context=commit_context,
        related_issues_context=related_context)
    diagnosis = stream_diagnosis(issue_text, system_prompt)
    diagnosis = validate_confidence(diagnosis, repo_context)

    # On-demand retry: if cited files exist on disk but weren't retrieved,
    # embed them now and re-diagnose once. Bounded to 1 retry.
    if rag_conn is not None and repo_root is not None:
        missing = get_missing_cited_files(diagnosis, repo_context, repo_root)
        if missing:
            console.print(f"[yellow]On-demand indexing {len(missing)} cited file(s)...[/]")
            for f in missing:
                console.print(f"  [dim]{f}[/]")
            from pathlib import Path
            from config import TOP_K_CHUNKS
            added = embed_files_on_demand(missing, Path(repo_root), rag_conn)
            if added > 0:
                console.print("[bold cyan]Re-retrieving with updated index...[/]")
                chunks, _ = hybrid_retrieve(
                    issue_text, skill_keywords, rag_conn, top_k=TOP_K_CHUNKS
                )
                if chunks:
                    repo_context = format_retrieved_chunks(chunks)
                    # Re-extract file list from updated chunks
                    retry_files = list(dict.fromkeys(
                        c["filepath"] for c in chunks))
                    commit_context = get_commit_history_context(retry_files, repo_root)
                    related_context = get_related_issues_context(
                        issue_text, retry_files, args.url or "")
                    system_prompt = build_system_prompt(
                        skill_contents, repo_context, used_rag,
                        issue_text=issue_text, crash_context=crash_context,
                        commit_context=commit_context,
                        related_issues_context=related_context)
                    console.print("[bold cyan]Re-diagnosing with retrieved source...[/]")
                    diagnosis = stream_diagnosis(issue_text, system_prompt)
                    diagnosis = validate_confidence(diagnosis, repo_context)
        if rag_conn:
            try:
                rag_conn.close()
            except Exception:
                pass

    if args.save_to:
        with open(args.save_to, "w", encoding="utf-8") as f:
            f.write(diagnosis)
    elif not args.no_save:
        save_report(diagnosis, issue_text, skill_names, used_rag)
    console.print()
    console.print(Rule("[dim]Done[/]"))


if __name__ == "__main__":
    main()