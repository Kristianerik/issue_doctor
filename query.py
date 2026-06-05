"""
query.py - Issue fetching and repo/RAG context resolution.

Depends on: config.py, rag.py
"""

import re
from pathlib import Path

import requests

import config
from config import (
    GITHUB_TOKEN, OLLAMA_HOST, EMBED_MODEL, SKIP_DIRS,
    INDEXABLE_EXTENSIONS, TOP_K_CHUNKS,
)
from rag import (
    SQLITE_VEC_AVAILABLE,
    build_git_activity_map, build_dir_keyword_map,
    build_index, check_embed_model_availability,
    extract_keywords_from_issue, format_retrieved_chunks,
    get_index_path, hybrid_retrieve, index_is_fresh,
    init_index_schema, keyword_search, open_index, score_file,
)

from config import console


# ── GitHub ─────────────────────────────────────────────────────────────────────

def parse_github_url(url):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    if not m:
        raise ValueError(f"Cannot parse: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_github_issue(url):
    owner, repo, number = parse_github_url(url)
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    with console.status("[bold cyan]Fetching issue from GitHub...[/]"):
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
            headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        comments = requests.get(data["comments_url"], headers=headers, timeout=15).json()
    parts = [
        f"Repository: {owner}/{repo}",
        f"Issue #{number}: {data['title']}",
        f"State: {data['state']}",
        f"Labels: {', '.join(l['name'] for l in data.get('labels', [])) or 'none'}",
        "", "## Issue Body", data.get("body") or "(no body)",
    ]
    if comments:
        parts.append("\n## Comments (up to 5)")
        for c in comments[:5]:
            parts.append(f"\n**@{c['user']['login']}:**\n{c['body']}")
    return "\n".join(parts)


# ── Repo detection ─────────────────────────────────────────────────────────────

def detect_git_root(start):
    current = start.resolve()
    for parent in [current] + list(current.parents):
        if (parent / ".git").exists():
            return parent
    return None


# ── Keyword fallback scan ──────────────────────────────────────────────────────

def keyword_scan_fallback(repo_root, issue_text, skill_keywords, skill_contents,
                          max_files=25):
    issue_kws = extract_keywords_from_issue(issue_text)
    all_kws = list(dict.fromkeys(skill_keywords + issue_kws))
    skill_text = " ".join(skill_contents).lower()
    all_files = []
    for f in repo_root.rglob("*"):
        try:
            is_file = f.is_file()
        except (OSError, PermissionError):
            continue
        if is_file and f.suffix in INDEXABLE_EXTENSIONS:
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            all_files.append(f)
    all_kws_set = {k.lower() for k in all_kws}
    scored = sorted(all_files,
                    key=lambda f: score_file(f, repo_root, all_kws_set),
                    reverse=True)
    mentioned = [f for f in scored[:max_files]
                 if f.name.lower() in skill_text
                 or f.parent.name.lower() + "/" + f.name.lower() in skill_text]
    other = [f for f in scored[:max_files] if f not in mentioned]
    content_files = (mentioned + other)[:5]
    console.print(f"[bold cyan]Keyword scan:[/] reading {len(content_files)} files")
    contents_block = ""
    total = 0
    for f in content_files:
        if total >= 24000:
            break
        rel = f.relative_to(repo_root)
        console.print(f"[dim]  {rel}[/]")
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        numbered = "\n".join("{:4d}  {}".format(i+1, l) for i, l in enumerate(lines))
        if len(numbered) > 8000:
            numbered = numbered[:8000] + "\n...(truncated)"
        remaining = 24000 - total
        if len(numbered) > remaining:
            numbered = numbered[:remaining]
        total += len(numbered)
        contents_block += "\n### {}\n```\n{}\n```\n".format(rel, numbered)
    return "## Key File Contents (keyword scan)\n" + contents_block


# ── Repo context resolver ──────────────────────────────────────────────────────

def resolve_repo_context(args, issue_text, skill_contents, skill_keywords):
    if args.no_repo:
        return None, False
    repo_root = None
    if args.repo:
        repo_root = Path(args.repo).resolve()
        if not repo_root.exists():
            console.print(f"[red]--repo path does not exist: {repo_root}[/]")
            return None, False
    else:
        repo_root = detect_git_root(Path.cwd())
        if not repo_root:
            console.print("[dim]No local git repo detected.[/]")
            return None, False
        console.print(f"[dim]Auto-detected repo: {repo_root}[/]")

    if SQLITE_VEC_AVAILABLE and check_embed_model_availability():
        index_path = get_index_path(repo_root)
        conn = open_index(index_path, create=True)
        if conn:
            init_index_schema(conn)
            needs_index = args.reindex or args.update or not index_is_fresh(conn, repo_root)
            if needs_index:
                force_rebuild = args.reindex or (not args.update and not index_is_fresh(conn, repo_root))
                build_index(repo_root, conn, force=force_rebuild,
                            max_index_files=args.max_files,
                            issue_text=issue_text,
                            skill_keywords=skill_keywords)
            else:
                stored = conn.execute(
                    "SELECT value FROM meta WHERE key='total_chunks'"
                ).fetchone()
                console.print(f"[green]Using cached RAG index ({stored[0] if stored else '?'} chunks)[/]")
                try:
                    kw_count = conn.execute("SELECT COUNT(*) FROM dir_keywords").fetchone()[0]
                    if kw_count == 0:
                        console.print("[dim]  Building missing dir maps...[/]")
                        build_git_activity_map(repo_root, conn)
                        build_dir_keyword_map(repo_root, conn)
                except Exception:
                    build_git_activity_map(repo_root, conn)
                    build_dir_keyword_map(repo_root, conn)

            console.print("[bold cyan]Hybrid retrieval (vector + keyword)...[/]")
            chunks, extracted_kws = hybrid_retrieve(
                issue_text, skill_keywords, conn, top_k=TOP_K_CHUNKS
            )
            if chunks:
                files_found = sorted(set(c["filepath"] for c in chunks))
                kw_hits = sum(1 for c in chunks if "keyword" in c.get("retrieval", ""))
                console.print(
                    f"[green]Retrieved {len(chunks)} chunks from "
                    f"{len(files_found)} files "
                    f"({kw_hits} keyword hits)[/]"
                )
                for f in files_found:
                    console.print(f"  [dim]{f}[/]")
                context = format_retrieved_chunks(chunks)
                conn.close()
                return context, True
            else:
                console.print("[yellow]Hybrid retrieval returned no chunks.[/]")
                conn.close()

    console.print("[dim]Using keyword-based file scan (no RAG).[/]")
    context = keyword_scan_fallback(repo_root, issue_text, skill_keywords, skill_contents)
    return context, False