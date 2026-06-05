#!/usr/bin/env python3
"""
eval_rag.py - Evaluate RAG retrieval quality for issue_doctor.

Usage:
  python eval_rag.py --repo /path/to/llvm-project --quick --fetch-issues
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
import time
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
    from rich.table import Table
    from rich.panel import Panel
    from rich.rule import Rule
except ImportError:
    print("pip install requests rich sqlite-vec")
    sys.exit(1)

try:
    import sqlite_vec
except ImportError:
    print("pip install sqlite-vec")
    sys.exit(1)

console = Console()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.environ.get("EMBED_MODEL",  "nomic-embed-text")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ── Ground truth ──────────────────────────────────────────────────────────────
GROUND_TRUTH = [
    {
        "issue_number": 199506,
        "issue_url": "https://github.com/llvm/llvm-project/issues/199506",
        "fix_commit": "35bfc00c5c8d",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineAndOrXor.cpp"],
        "fix_functions": ["foldBitmaskMul", "matchBitmaskMul"],
        "description": "[InstCombine] Fix type mismatch in foldBitmaskMul",
        "issue_text": "",
    },
    {
        "issue_number": 198389,
        "issue_url": "https://github.com/llvm/llvm-project/issues/198389",
        "fix_commit": "c74478738139",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineCompares.cpp"],
        "fix_functions": ["visitFCmpInst", "foldFCmpWithIntCast"],
        "description": "[InstCombine] Do not crash in compare of bitcast pattern",
        "issue_text": "",
    },
    {
        "issue_number": 199401,
        "issue_url": "https://github.com/llvm/llvm-project/issues/199401",
        "fix_commit": "46666d99e035",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineCalls.cpp"],
        "fix_functions": ["visitCallInst", "foldReductionIdiom"],
        "description": "[InstCombine] Fix vector_reduce_mul(sext <n x i1>) for odd n",
        "issue_text": "",
    },
    {
        "issue_number": 170072,
        "issue_url": "https://github.com/llvm/llvm-project/issues/170072",
        "fix_commit": "afe61654c0e8",
        "fix_files": ["clang/lib/Sema/SemaChecking.cpp"],
        "fix_functions": ["CheckForIntOverflow", "EvaluateForOverflow"],
        "description": "[Clang][Sema] Fix crash EvaluateForOverflow for UnaryOperator",
        "issue_text": "",
    },
    {
        "issue_number": 200263,
        "issue_url": "https://github.com/llvm/llvm-project/issues/200263",
        "fix_commit": "3ca81aa6b6d0",
        "fix_files": ["llvm/lib/Transforms/Vectorize/VectorCombine.cpp"],
        "fix_functions": ["scalarizeLoad", "runImpl"],
        "description": "[VectorCombine] Don't scalarize atomic loads",
        "issue_text": "",
    },
]

EVAL_CONFIGS = [
    {"chunk_lines": 60,  "overlap": 10, "label": "medium-tight"},
    {"chunk_lines": 60,  "overlap": 20, "label": "medium-loose"},
    {"chunk_lines": 30,  "overlap": 5,  "label": "small-tight"},
    {"chunk_lines": 40,  "overlap": 10, "label": "small-loose"},
    {"chunk_lines": 100, "overlap": 10, "label": "large-tight"},
    {"chunk_lines": 100, "overlap": 20, "label": "large-loose"},
]
TOP_K_VALUES = [5, 10, 15, 20]

# ── Embedding ─────────────────────────────────────────────────────────────────
_EMBED_URL = ""
_EMBED_FMT = ""

def detect_embed_api():
    global _EMBED_URL, _EMBED_FMT
    if _EMBED_URL:
        return _EMBED_URL, _EMBED_FMT
    for url, fmt, payload in [
        (f"{OLLAMA_HOST}/api/embed",      "new",  {"model": EMBED_MODEL, "input": ["test"]}),
        (f"{OLLAMA_HOST}/api/embed",      "new1", {"model": EMBED_MODEL, "input": "test"}),
        (f"{OLLAMA_HOST}/api/embeddings", "old",  {"model": EMBED_MODEL, "prompt": "test"}),
    ]:
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                d = r.json()
                if "embeddings" in d or "embedding" in d:
                    _EMBED_URL, _EMBED_FMT = url, fmt
                    return url, fmt
        except Exception:
            continue
    return "", ""

def embed_texts(texts):
    if not texts: return []
    url, fmt = detect_embed_api()
    if not url: return [[] for _ in texts]
    try:
        if fmt == "new":
            r = requests.post(url, json={"model": EMBED_MODEL, "input": texts},
                              timeout=max(120, len(texts)*5))
            if r.status_code == 200:
                return r.json().get("embeddings", [[] for _ in texts])
        results = []
        for t in texts:
            p = {"model": EMBED_MODEL, "input": t} if fmt == "new1" else {"model": EMBED_MODEL, "prompt": t}
            r = requests.post(url, json=p, timeout=30)
            d = r.json() if r.status_code == 200 else {}
            emb = d.get("embeddings", [[]])[0] if fmt == "new1" else d.get("embedding", [])
            results.append(emb)
        return results
    except Exception as e:
        console.print(f"[red]Embed failed: {e}[/]")
        return [[] for _ in texts]

def embed_single(text):
    r = embed_texts([text]); return r[0] if r else []

def get_embed_dim():
    emb = embed_single("test"); return len(emb) if emb else 768

# ── Chunking ──────────────────────────────────────────────────────────────────
def chunk_file(filepath, repo_root, chunk_lines, overlap):
    try:
        text = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    lines = text.splitlines()
    if not lines: return []
    rel = str(filepath.relative_to(repo_root)).replace("\\", "/")
    func_pat = re.compile(
        r"^(?:pub\s+)?(?:static\s+)?(?:inline\s+)?(?:async\s+)?"
        r"(?:def |fn |func |function |class |\w[\w\s\*:<>&,]*\s+\w+\s*\()"
    )
    bounds = [0]
    for i, line in enumerate(lines):
        if i > 0 and func_pat.match(line.strip()):
            bounds.append(i)
    bounds.append(len(lines))
    chunks = []
    if len(bounds) > 2:
        for idx in range(len(bounds)-1):
            s = max(0, bounds[idx]-overlap); e = bounds[idx+1]
            content = "\n".join(f"{s+k+1:4d}  {l}" for k,l in enumerate(lines[s:e]))
            chunks.append({"filepath": rel, "start_line": s+1, "end_line": e, "content": content})
    else:
        i = 0
        while i < len(lines):
            e = min(i+chunk_lines, len(lines))
            content = "\n".join(f"{i+k+1:4d}  {l}" for k,l in enumerate(lines[i:e]))
            chunks.append({"filepath": rel, "start_line": i+1, "end_line": e, "content": content})
            i += chunk_lines - overlap
    return chunks

# ── Index ─────────────────────────────────────────────────────────────────────
def build_eval_index(repo_root, target_files, chunk_lines, overlap, embed_dim):
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    conn.executescript(f"""
        CREATE TABLE chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT, start_line INTEGER, end_line INTEGER, content TEXT);
        CREATE VIRTUAL TABLE chunk_embeddings USING vec0(embedding FLOAT[{embed_dim}]);
    """)
    all_chunks = []
    for rel in target_files:
        f = repo_root / rel.replace("/", os.sep)
        if f.exists(): all_chunks.extend(chunk_file(f, repo_root, chunk_lines, overlap))
    if not all_chunks: return conn, 0
    total = 0
    for i in range(0, len(all_chunks), 16):
        batch = all_chunks[i:i+16]
        embs = embed_texts([c["content"][:800] for c in batch])
        for chunk, emb in zip(batch, embs):
            if not emb or len(emb) != embed_dim: continue
            cur = conn.execute(
                "INSERT INTO chunks(filepath,start_line,end_line,content) VALUES(?,?,?,?)",
                (chunk["filepath"], chunk["start_line"], chunk["end_line"], chunk["content"]))
            conn.execute("INSERT INTO chunk_embeddings(rowid,embedding) VALUES(?,?)",
                         (cur.lastrowid, struct.pack(f"{len(emb)}f", *emb)))
            total += 1
    conn.commit(); return conn, total

def retrieve_top_k(query, conn, embed_dim, k):
    emb = embed_single(query[:2000])
    if not emb or len(emb) != embed_dim: return []
    blob = struct.pack(f"{len(emb)}f", *emb)
    rows = conn.execute("""
        SELECT c.filepath, c.start_line, c.end_line, ce.distance
        FROM chunk_embeddings ce JOIN chunks c ON c.id=ce.rowid
        WHERE ce.embedding MATCH ? AND k=? ORDER BY ce.distance
    """, (blob, k)).fetchall()
    return [{"filepath": r[0], "start_line": r[1], "end_line": r[2], "distance": r[3]} for r in rows]

# ── Metrics ───────────────────────────────────────────────────────────────────
def compute_recall(retrieved, gt_files, k):
    hits = {c["filepath"] for c in retrieved[:k]} & set(gt_files)
    return len(hits) > 0, hits

def compute_mrr(retrieved, gt_files):
    gt = set(gt_files)
    for i, c in enumerate(retrieved):
        if c["filepath"] in gt: return 1.0/(i+1)
    return 0.0

def compute_coverage(retrieved, gt_files, k):
    top = retrieved[:k]
    if not top: return 0.0
    return sum(1 for c in top if c["filepath"] in set(gt_files)) / len(top)

# ── GitHub ────────────────────────────────────────────────────────────────────
def fetch_issue_text(issue_number, repo="llvm/llvm-project"):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN: headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    for ep in ["issues", "pulls"]:
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}/{ep}/{issue_number}",
                             headers=headers, timeout=15)
            if int(r.headers.get("X-RateLimit-Remaining", 60)) == 0:
                console.print("[yellow]Rate limited.[/]"); return ""
            if r.status_code == 404: continue
            r.raise_for_status()
            d = r.json()
            parts = [f"Issue #{issue_number}: {d['title']}",
                     f"Labels: {', '.join(l['name'] for l in d.get('labels',[]))}",
                     "", "## Body", d.get("body") or ""]
            if ep == "issues":
                comments = requests.get(d["comments_url"], headers=headers, timeout=15).json()
                if isinstance(comments, list):
                    for c in comments[:3]:
                        parts.append(f"\n@{c['user']['login']}: {c['body'][:300]}")
            return "\n".join(parts)
        except Exception as e:
            console.print(f"[yellow]Fetch #{issue_number}/{ep}: {e}[/]"); continue
    return ""

def load_cache(f): return json.loads(Path(f).read_text()) if Path(f).exists() else {}
def save_cache(cache, f): Path(f).write_text(json.dumps(cache, indent=2))

# ── Eval ──────────────────────────────────────────────────────────────────────
def run_eval(repo_root, configs, cases, embed_dim, top_k_values):
    results = []
    console.print("[dim]Collecting distractor files...[/]")
    distractor_dir = repo_root / "llvm" / "lib" / "Transforms" / "InstCombine"
    distractors = ([str(f.relative_to(repo_root)).replace("\\","/")
                    for f in distractor_dir.glob("*.cpp")]
                   if distractor_dir.exists() else [])
    console.print(f"[dim]  {len(distractors)} InstCombine files as distractor pool[/]")

    for cfg in configs:
        console.print(); console.print(Rule(
            f"[bold cyan]Config: {cfg['label']} (chunk={cfg['chunk_lines']} lines, overlap={cfg['overlap']})[/]"))
        cfg_res = {"config": cfg, "cases": []}
        for case in cases:
            if not case["issue_text"]:
                console.print(f"[yellow]  Skipping #{case['issue_number']}[/]"); continue
            console.print(f"\n  Case #{case['issue_number']}: {case['description'][:60]}")
            index_files = list(set(case["fix_files"] + distractors))
            console.print(f"  [dim]Indexing {len(index_files)} files...[/]")
            conn, n = build_eval_index(repo_root, index_files, cfg["chunk_lines"], cfg["overlap"], embed_dim)
            console.print(f"  [dim]{n} chunks embedded[/]")
            retrieved = retrieve_top_k(case["issue_text"][:2000], conn, embed_dim, max(top_k_values))
            conn.close()
            case_res = {"issue_number": case["issue_number"], "description": case["description"],
                        "fix_files": case["fix_files"], "top_k_results": {}}
            for k in top_k_values:
                hit, hit_files = compute_recall(retrieved, case["fix_files"], k)
                mrr = compute_mrr(retrieved, case["fix_files"])
                cov = compute_coverage(retrieved, case["fix_files"], k)
                case_res["top_k_results"][k] = {"hit": hit, "hit_files": list(hit_files),
                                                 "mrr": round(mrr,3), "coverage": round(cov,3)}
                console.print(f"    top-{k:2d}: {'[green]HIT[/]' if hit else '[red]MISS[/]'}  MRR={mrr:.2f}  coverage={cov:.0%}")
            if retrieved:
                console.print("  [dim]Top 5 retrieved:[/]")
                for i, c in enumerate(retrieved[:5]):
                    marker = " <-- TARGET" if c["filepath"] in case["fix_files"] else ""
                    console.print(f"    {i+1}. {c['filepath']} (L{c['start_line']}-{c['end_line']}, sim={1-c['distance']:.2f}){marker}")
            cfg_res["cases"].append(case_res)
        results.append(cfg_res)
    return results

def print_summary(results, top_k_values):
    console.print(); console.print(Rule("[bold]Summary[/]")); console.print()
    for k in top_k_values:
        t = Table(title=f"Recall@{k}")
        t.add_column("Config", style="cyan"); t.add_column("Chunk", justify="right"); t.add_column("Overlap", justify="right")
        for cr in results[:1]:
            for case in cr["cases"]: t.add_column(f"#{case['issue_number']}", justify="center")
        t.add_column("Avg Recall", justify="right"); t.add_column("Avg MRR", justify="right")
        for cr in results:
            cfg = cr["config"]; row = [cfg["label"], str(cfg["chunk_lines"]), str(cfg["overlap"])]
            hits, mrrs = [], []
            for case in cr["cases"]:
                kr = case["top_k_results"].get(k, {})
                hits.append(kr.get("hit", False)); mrrs.append(kr.get("mrr", 0))
                row.append("[green]Y[/]" if kr.get("hit") else "[red]N[/]")
            row += [f"{sum(hits)/len(hits):.0%}" if hits else "0%",
                    f"{sum(mrrs)/len(mrrs):.2f}" if mrrs else "0.00"]
            t.add_row(*row)
        console.print(t); console.print()

def recommend(results, top_k=10):
    best_score, best_cfg = -1, None
    for cr in results:
        hits, mrrs = [], []
        for case in cr["cases"]:
            avail = sorted(case["top_k_results"].keys())
            k = top_k if top_k in avail else (avail[-1] if avail else None)
            if k is None: continue
            kr = case["top_k_results"][k]
            hits.append(1 if kr.get("hit") else 0); mrrs.append(kr.get("mrr", 0.0))
        if not hits: continue
        score = (sum(hits)/len(hits))*0.6 + (sum(mrrs)/len(mrrs))*0.4
        if score > best_score: best_score, best_cfg = score, cr["config"]
    return best_cfg, best_score

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Evaluate RAG configs for issue_doctor.")
    p.add_argument("--repo",         metavar="PATH")
    p.add_argument("--fetch-issues", action="store_true")
    p.add_argument("--show-cases",   action="store_true")
    p.add_argument("--cache",        default="eval_issue_cache.json")
    p.add_argument("--configs",      metavar="NAMES")
    p.add_argument("--top-k",        default="5,10,15,20")
    p.add_argument("--quick",        action="store_true")
    p.add_argument("--output",       metavar="FILE")
    args = p.parse_args()

    if args.show_cases:
        for case in GROUND_TRUTH:
            console.print(f"\n#{case['issue_number']}: {case['description']}")
            console.print(f"  Files: {case['fix_files']}")
        sys.exit(0)

    cache = load_cache(args.cache)
    for case in GROUND_TRUTH:
        if str(case["issue_number"]) in cache:
            case["issue_text"] = cache[str(case["issue_number"])]

    if args.fetch_issues:
        console.print("[bold cyan]Fetching issue text from GitHub...[/]")
        for case in GROUND_TRUTH:
            n = case["issue_number"]
            if case["issue_text"]: console.print(f"  #{n}: already cached"); continue
            console.print(f"  #{n}: fetching...")
            text = fetch_issue_text(n)
            if text: case["issue_text"] = text; cache[str(n)] = text; console.print(f"  #{n}: OK ({len(text)} chars)")
            else: console.print(f"  #{n}: FAILED")
            time.sleep(1)
        save_cache(cache, args.cache)
        console.print(f"[green]Cached to {args.cache}[/]")

    cases = [c for c in GROUND_TRUTH if c["issue_text"]]
    if not cases:
        console.print("[yellow]No issue text. Run --fetch-issues first.[/]"); sys.exit(0)
    console.print(f"[green]{len(cases)}/{len(GROUND_TRUTH)} cases have issue text[/]")

    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    if not (repo_root/".git").exists():
        console.print(f"[red]No .git at {repo_root}[/]"); sys.exit(1)
    console.print(f"Repo: {repo_root}")

    console.print("[dim]Detecting embedding dimensions...[/]")
    embed_dim = get_embed_dim()
    if not embed_dim:
        console.print("[red]Cannot embed — is Ollama running?[/]"); sys.exit(1)
    console.print(f"[dim]Embedding dimensions: {embed_dim}[/]")

    configs = EVAL_CONFIGS
    if args.quick: configs = [c for c in EVAL_CONFIGS if "medium" in c["label"]]
    elif args.configs: configs = [c for c in EVAL_CONFIGS if c["label"] in set(args.configs.split(","))]

    top_k_values = [int(x) for x in args.top_k.split(",")]
    console.print(); console.print(Panel.fit(
        f"[bold cyan]RAG Eval[/]\n[dim]{len(cases)} cases  |  {len(configs)} configs  |  top-k: {top_k_values}[/]",
        border_style="cyan"))

    results = run_eval(repo_root, configs, cases, embed_dim, top_k_values)
    print_summary(results, top_k_values)

    best_cfg, score = recommend(results)
    if best_cfg:
        console.print(f"[bold green]Recommended config:[/] {best_cfg['label']}")
        console.print(f"  chunk_lines={best_cfg['chunk_lines']}  overlap={best_cfg['overlap']}")
        console.print(f"  score={score:.2f} (recall*0.6 + MRR*0.4)")
        console.print(f"\nTo apply in issue_doctor.py, update:")
        console.print(f"  CHUNK_SIZE_LINES = {best_cfg['chunk_lines']}")
        console.print(f"  CHUNK_OVERLAP    = {best_cfg['overlap']}")

    out = args.output or f"eval_results_{int(time.time())}.json"
    with open(out, "w") as f: json.dump(results, f, indent=2, default=str)
    console.print(f"\n[dim]Full results saved to {out}[/]")

if __name__ == "__main__":
    main()