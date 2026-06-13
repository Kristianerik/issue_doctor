"""
rag.py - RAG index: embedding, chunking, indexing, and retrieval.

Depends on: config.py
"""

import datetime
import os
import re
import sqlite3
import struct
import subprocess
from pathlib import Path
from typing import Optional

import requests

from config import (
    CHUNK_OVERLAP, CHUNK_SIZE_LINES, EMBED_DIMENSIONS, EMBED_MODEL,
    GENERIC_GOOD_PATHS, INDEX_DB_NAME, INDEX_DIR_NAME, INDEXABLE_EXTENSIONS,
    MAX_CONTEXT_CHARS, NOISE_DIRS, OLLAMA_HOST, PRIMARY_REPO_NAMES,
    SKIP_DIRS, SOURCE_DIRS, TOP_K_CHUNKS, _DOC_NAME_PATTERNS, _TOOL_FILENAME,
)

try:
    import sqlite_vec
    SQLITE_VEC_AVAILABLE = True
except ImportError:
    SQLITE_VEC_AVAILABLE = False

from config import console


# ── Doc-file detection ────────────────────────────────────────────────────────

def is_doc_file(filepath: str) -> bool:
    """
    Returns True if the file looks like documentation rather than source.
    Pattern-based — works on any repo without hardcoded project names.
    """
    parts = filepath.lower().replace("\\", "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0] if parts else ""
    if any(pat in stem for pat in _DOC_NAME_PATTERNS):
        return True
    if any(any(pat in part for pat in _DOC_NAME_PATTERNS) for part in parts[:-1]):
        return True
    return False


# ── Vector helpers ─────────────────────────────────────────────────────────────

def vec_to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)


# ── Embedding ──────────────────────────────────────────────────────────────────

_EMBED_URL = ""
_EMBED_FORMAT = ""


def _detect_embed_api():
    global _EMBED_URL, _EMBED_FORMAT
    if _EMBED_URL:
        return _EMBED_URL, _EMBED_FORMAT
    candidates = [
        (f"{OLLAMA_HOST}/api/embed",      "new",  {"model": EMBED_MODEL, "input": ["test"]}),
        (f"{OLLAMA_HOST}/api/embed",      "new1", {"model": EMBED_MODEL, "input": "test"}),
        (f"{OLLAMA_HOST}/api/embeddings", "old",  {"model": EMBED_MODEL, "prompt": "test"}),
    ]
    for url, fmt, payload in candidates:
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "embeddings" in data or "embedding" in data:
                    _EMBED_URL, _EMBED_FORMAT = url, fmt
                    console.print(f"[dim]Embed API: {url} ({fmt})[/]")
                    return url, fmt
        except Exception:
            continue
    console.print("[red]Could not detect working Ollama embed API.[/]")
    return "", ""


def embed_texts(texts):
    if not texts:
        return []
    url, fmt = _detect_embed_api()
    if not url:
        return [[] for _ in texts]
    try:
        if fmt == "new":
            resp = requests.post(url, json={"model": EMBED_MODEL, "input": texts},
                                 timeout=max(120, len(texts) * 5))
            if resp.status_code == 200:
                return resp.json().get("embeddings", [[] for _ in texts])
        results = []
        for t in texts:
            if fmt == "new1":
                resp = requests.post(url, json={"model": EMBED_MODEL, "input": t}, timeout=30)
                if resp.status_code == 200:
                    embs = resp.json().get("embeddings", [[]])
                    results.append(embs[0] if embs else [])
                else:
                    results.append([])
            else:
                resp = requests.post(url, json={"model": EMBED_MODEL, "prompt": t}, timeout=30)
                results.append(resp.json().get("embedding", []) if resp.status_code == 200 else [])
        return results
    except Exception as e:
        console.print(f"[red]Embedding failed: {e}[/]")
        return [[] for _ in texts]


def embed_single(text):
    r = embed_texts([text])
    return r[0] if r else []


def check_embed_model_availability() -> bool:
    """Check if the configured embed model is available in Ollama."""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        short = EMBED_MODEL.split(":")[0].lower()
        if any(short in m.lower() for m in models):
            return True
        console.print(f"[yellow]Embed model '{EMBED_MODEL}' not found.[/]")
        console.print(f"[dim]Available: {', '.join(models)}[/]")
        console.print(f"[dim]Run: ollama pull {EMBED_MODEL}[/]")
        return False
    except requests.exceptions.Timeout:
        console.print("[yellow]Ollama timeout — using keyword fallback.[/]")
        return False
    except Exception as e:
        console.print(f"[yellow]Embed check failed ({type(e).__name__}) — keyword fallback.[/]")
        return False


# ── File scoring ───────────────────────────────────────────────────────────────

def extract_keywords_from_issue(issue_text):
    cleaned = re.sub(r"https?://\S+", "", issue_text)
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    words = re.findall(r"\b([a-zA-Z_]\w{3,})\b", cleaned)
    stopwords = {"this","that","with","from","have","when","where","which",
                 "there","their","about","would","could","should","will",
                 "also","more","than","then","into"}
    seen, result = set(), []
    for w in words:
        wl = w.lower()
        if wl not in stopwords and wl not in seen:
            seen.add(wl); result.append(wl)
    return result[:80]


def score_file(filepath, repo_root, all_kws):
    if filepath.name == _TOOL_FILENAME:
        return -1
    path_lower = str(filepath).lower().replace("\\", "/")
    parts = set(path_lower.replace(str(repo_root).lower().replace("\\","/"), "").split("/"))
    name = filepath.name.lower()
    stem = filepath.stem.lower()

    if parts & NOISE_DIRS:
        return -1

    source_boost  = 3 if parts & SOURCE_DIRS else 0
    primary_boost = 2 if any(name_part in path_lower.split("/")[0]
                              for name_part in PRIMARY_REPO_NAMES) else 0
    issue_hits    = sum(3 for kw in all_kws if len(kw) > 4 and (kw in name or kw in stem))
    path_hits     = sum(1 for kw in all_kws if len(kw) > 4 and kw in path_lower)
    generic_hits  = len(parts & GENERIC_GOOD_PATHS)

    return source_boost + primary_boost + issue_hits + path_hits + generic_hits


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_file(filepath, repo_root):
    try:
        text = filepath.read_text(encoding="utf-8", errors="ignore")
    except (OSError, PermissionError):
        return []
    lines = text.splitlines()
    if not lines:
        return []
    rel = str(filepath.relative_to(repo_root)).replace("\\", "/")
    func_pattern = re.compile(
        r"^(?:pub\s+)?(?:static\s+)?(?:inline\s+)?(?:async\s+)?"
        r"(?:def |fn |func |function |class |\w[\w\s\*:<>&,]*\s+\w+\s*\()"
    )
    c_func_pattern = re.compile(
        r"^(?!(?:if|for|while|switch|return|else|do|case|break|continue|goto)\b)"
        r"[A-Za-z_][\w]*\s*\([^;]*$"
    )
    is_c_file = filepath.suffix in {'.c', '.h'}
    boundaries = [0]
    for i, line in enumerate(lines):
        if i > 0:
            stripped = line.strip()
            if func_pattern.match(stripped):
                boundaries.append(i)
            elif is_c_file and c_func_pattern.match(stripped):
                boundaries.append(i)
    boundaries.append(len(lines))
    chunks = []
    if len(boundaries) > 2:
        for idx in range(len(boundaries) - 1):
            start = max(0, boundaries[idx] - CHUNK_OVERLAP)
            end   = boundaries[idx + 1]
            content = "\n".join(
                "{:4d}  {}".format(start + k + 1, l)
                for k, l in enumerate(lines[start:end])
            )
            chunks.append({"filepath": rel, "start_line": start+1,
                           "end_line": end, "content": content})
    else:
        i = 0
        while i < len(lines):
            end = min(i + CHUNK_SIZE_LINES, len(lines))
            content = "\n".join(
                "{:4d}  {}".format(i+k+1, l) for k, l in enumerate(lines[i:end])
            )
            chunks.append({"filepath": rel, "start_line": i+1,
                           "end_line": end, "content": content})
            i += CHUNK_SIZE_LINES - CHUNK_OVERLAP
    return chunks


# ── Index management ───────────────────────────────────────────────────────────

def get_index_path(repo_root):
    return repo_root / INDEX_DIR_NAME / INDEX_DB_NAME


def open_index(index_path, create=False):
    if not SQLITE_VEC_AVAILABLE:
        return None
    if not create and not index_path.exists():
        return None
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(index_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_index_schema(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings
        USING vec0(embedding FLOAT[{EMBED_DIMENSIONS}]);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS dir_keywords (
            directory TEXT NOT NULL,
            keyword   TEXT NOT NULL,
            weight    INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_dir_kw ON dir_keywords(keyword);
        CREATE TABLE IF NOT EXISTS dir_activity (
            directory    TEXT PRIMARY KEY,
            commit_count INTEGER NOT NULL DEFAULT 0,
            last_commit  TEXT
        );
        CREATE TABLE IF NOT EXISTS file_versions (
            filepath     TEXT PRIMARY KEY,
            commit_hash  TEXT NOT NULL,
            indexed_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fv_filepath ON file_versions(filepath);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(content, filepath, content=chunks, content_rowid=id);
    """)
    conn.commit()


def get_repo_commit_hash(repo_root):
    try:
        head = (repo_root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref_path = repo_root / ".git" / head[5:]
            if ref_path.exists():
                return ref_path.read_text().strip()[:12]
        return head[:12]
    except Exception:
        return "unknown"


def get_file_commit_hashes_batch(repo_root: Path, filepaths: list) -> dict:
    """Get commit hashes for multiple files in one git log call."""
    if not filepaths:
        return {}
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--name-only", "--diff-filter=AM", "-5000"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=60
        )
        if result.returncode != 0:
            return {}
        file_to_hash: dict[str, str] = {}
        current_hash = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                current_hash = line[:12]
            elif current_hash and "/" in line:
                norm = line.replace("\\", "/")
                if norm not in file_to_hash:
                    file_to_hash[norm] = current_hash
        result_map = {}
        for fp in filepaths:
            rel = str(fp.relative_to(repo_root)).replace("\\", "/")
            result_map[rel] = file_to_hash.get(rel, "unknown")
        return result_map
    except Exception:
        return {}


def index_is_fresh(conn, repo_root):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='commit_hash'").fetchone()
        return row and row[0] == get_repo_commit_hash(repo_root)
    except Exception:
        return False


def get_stale_files(conn, repo_root: Path, filepaths: list) -> tuple[list, list]:
    """Return (new_files, changed_files) for incremental indexing."""
    if not filepaths:
        return [], []
    current_hashes = get_file_commit_hashes_batch(repo_root, filepaths)
    stored = {}
    try:
        rows = conn.execute("SELECT filepath, commit_hash FROM file_versions").fetchall()
        stored = {r[0]: r[1] for r in rows}
    except Exception:
        pass
    new_files, changed_files = [], []
    for fp in filepaths:
        rel = str(fp.relative_to(repo_root)).replace("\\", "/")
        current = current_hashes.get(rel, "unknown")
        if rel not in stored:
            new_files.append(fp)
        elif stored[rel] != current:
            changed_files.append(fp)
    return new_files, changed_files


# ── Git activity map ───────────────────────────────────────────────────────────

def build_git_activity_map(repo_root: Path, conn: sqlite3.Connection, months: int = 6) -> None:
    """Build directory activity map from git history. Repo-agnostic."""
    import math
    console.print(f"[dim]  Building git activity map ({months} months)...[/]")
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--name-only", "--format=", "--diff-filter=AM"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=60,
        )
        if result.returncode != 0:
            console.print(f"[dim]  git log failed: {result.stderr[:80]}[/]")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        console.print(f"[dim]  git not available: {e}[/]")
        return

    dir_commits: dict[str, int] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        path = line.replace("\\", "/")
        directory = "/".join(path.split("/")[:-1]) if "/" in path else "."
        parts = directory.split("/")
        for depth in range(1, len(parts) + 1):
            parent = "/".join(parts[:depth])
            dir_commits[parent] = dir_commits.get(parent, 0) + 1

    if not dir_commits:
        console.print("[dim]  No git activity found[/]")
        return

    conn.execute("DELETE FROM dir_activity")
    conn.executemany(
        "INSERT OR REPLACE INTO dir_activity (directory, commit_count) VALUES (?, ?)",
        [(d, c) for d, c in dir_commits.items()]
    )
    conn.commit()
    console.print(
        f"[dim]  Git activity: {len(dir_commits)} dirs, "
        f"max {max(dir_commits.values())} file-changes in top dir[/]"
    )


# ── Directory auto-discovery ───────────────────────────────────────────────────

def _tokenise_identifier(name: str) -> list[str]:
    """Split identifier into keyword tokens. InstCombineCasts → [inst, combine, casts, ...]"""
    tokens = [name.lower()]
    parts = re.split(r'[_\-]', name)
    tokens.extend(p.lower() for p in parts if len(p) >= 3)
    camel_parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', name)
    tokens.extend(p.lower() for p in camel_parts if len(p) >= 3)
    for i in range(len(camel_parts) - 1):
        pair = camel_parts[i].lower() + camel_parts[i+1].lower()
        if len(pair) >= 5:
            tokens.append(pair)
    return list(set(tokens))


def build_dir_keyword_map(repo_root: Path, conn: sqlite3.Connection) -> None:
    """Build directory keyword map from repo structure. No hardcoded paths needed."""
    console.print("[dim]  Building directory keyword map...[/]")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dir_keywords (
            directory TEXT NOT NULL,
            keyword   TEXT NOT NULL,
            weight    INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_dir_kw ON dir_keywords(keyword);
        CREATE TABLE IF NOT EXISTS dir_activity (
            directory    TEXT PRIMARY KEY,
            commit_count INTEGER NOT NULL DEFAULT 0,
            last_commit  TEXT
        );
        DELETE FROM dir_keywords;
    """)

    dir_keywords: dict[str, set[str]] = {}
    for f in repo_root.rglob("*"):
        try:
            if not f.is_file():
                continue
        except (OSError, PermissionError):
            continue
        if f.suffix not in INDEXABLE_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        dir_path = "/".join(rel.split("/")[:-1]) or "."
        if dir_path not in dir_keywords:
            dir_keywords[dir_path] = set()
        for part in dir_path.split("/"):
            if len(part) >= 3:
                dir_keywords[dir_path].update(_tokenise_identifier(part))
        dir_keywords[dir_path].update(_tokenise_identifier(f.stem))

    rows = []
    for directory, keywords in dir_keywords.items():
        dir_tokens = set()
        for part in directory.split("/"):
            if len(part) >= 3:
                dir_tokens.update(tok.lower() for tok in _tokenise_identifier(part))
        for kw in keywords:
            kw_lower = kw.lower()
            if len(kw_lower) < 3:
                continue
            weight = 3 if kw_lower in dir_tokens else 1
            rows.append((directory, kw_lower, weight))

    conn.executemany(
        "INSERT INTO dir_keywords (directory, keyword, weight) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    console.print(f"[dim]  Dir keyword map: {len(dir_keywords)} directories, "
                  f"{len(rows)} keyword entries[/]")


def get_guaranteed_dirs_from_map(
    issue_text: str,
    skill_keywords: list[str],
    conn: sqlite3.Connection,
    repo_root: Path,
    top_n_dirs: int = 12,
) -> list[Path]:
    """Query dir keyword map to find most relevant directories for this issue.

    Dir discovery is driven by issue-text keywords only.
    Skill trigger keywords are intentionally excluded — they are broad domain
    terms (e.g. 'codegen', 'selectiondag') that would bias discovery toward
    dirs matching the skill's domain rather than the specific issue.
    """
    import math
    try:
        count = conn.execute("SELECT COUNT(*) FROM dir_keywords").fetchone()[0]
        if count == 0:
            return []
    except Exception:
        return []

    # Use augmented keywords for dir discovery — they include stacktrace
    # function names, CamelCase identifiers, and backtick contents that
    # simple word extraction would miss (e.g. SemaChecking, EvaluateForOverflow).
    # Skill keywords included as secondary signal, same as original.
    if issue_text:
        _, augmented_kws = augment_query(issue_text, [])
        from_issue = augmented_kws
    else:
        from_issue = extract_keywords_from_issue(issue_text) if issue_text else []
    all_kws_raw = list(dict.fromkeys(
        [k.lower() for k in (from_issue + skill_keywords)]
    ))[:60]

    GENERIC_NOISE = {
        'float', 'calls', 'convert', 'conversion', 'integer', 'value',
        'values', 'target', 'function', 'pointer', 'offset', 'result',
        'source', 'output', 'input', 'object', 'optimize', 'missed',
        'expressions', 'expression', 'pattern', 'optimization', 'failed',
        'issue', 'error', 'should', 'return', 'using', 'check',
    }
    # len>=6 threshold from original — keeps generic short skill terms
    # (codegen, llvm) from overwhelming specific issue identifiers (sema)
    specific_kws = [k for k in all_kws_raw if len(k) >= 6 and k not in GENERIC_NOISE]
    if not specific_kws:
        specific_kws = [k for k in all_kws_raw if len(k) >= 4]

    dir_scores: dict[str, float] = {}
    for kw in specific_kws:
        # Exact match
        rows = conn.execute(
            "SELECT directory, weight FROM dir_keywords WHERE keyword = ?", (kw,)
        ).fetchall()
        for (directory, weight) in rows:
            dir_scores[directory] = dir_scores.get(directory, 0) + weight


    if not dir_scores:
        return []

    noise = NOISE_DIRS | {"include"}
    filtered = {
        d: s for d, s in dir_scores.items()
        if not any(part in noise for part in d.split("/")) and s >= 1
    }

    dir_activity: dict[str, int] = {}
    try:
        matched_dirs = list(filtered.keys())
        if matched_dirs:
            placeholders = ",".join("?" * len(matched_dirs))
            rows = conn.execute(
                f"SELECT directory, commit_count FROM dir_activity "
                f"WHERE directory IN ({placeholders})",
                matched_dirs
            ).fetchall()
            dir_activity = {r[0]: r[1] for r in rows}
    except Exception:
        pass

    final_scores: dict[str, float] = {}
    for d, kw_score in filtered.items():
        commits = dir_activity.get(d, 0)
        capped_commits = min(commits, 500)
        activity_factor = math.log(1 + capped_commits) if capped_commits > 0 else 1.0
        final_scores[d] = (kw_score ** 2) * activity_factor

    top_dirs = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)[:top_n_dirs]

    if top_dirs:
        console.print("[dim]  Auto-discovered relevant directories:[/]")
        for d, score in top_dirs:
            kw = filtered.get(d, 0)
            commits = dir_activity.get(d, 0)
            console.print(f"[dim]    {d} (kw={kw}, commits={commits}, score={score:.1f})[/]")

    return [repo_root / d.replace("/", os.sep) for d, _ in top_dirs]


# ── Index builder ──────────────────────────────────────────────────────────────

def build_index(repo_root, conn, force=False, max_index_files=3000,
                issue_text="", skill_keywords=None):
    if skill_keywords is None:
        skill_keywords = []

    has_existing = False
    try:
        count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        has_existing = count > 0
    except Exception:
        pass

    stacktrace_files_missing = False
    if not force and has_existing and issue_text:
        strace_fnames = set(re.findall(
            r'\b([A-Za-z][\w]+\.(?:cpp|h|inc|c)):\d+', issue_text
        ))
        if strace_fnames:
            indexed = {
                row[0].split('/')[-1].split('\\')[-1]
                for row in conn.execute("SELECT DISTINCT filepath FROM chunks").fetchall()
            }
            stacktrace_files_missing = bool(strace_fnames - indexed)

    if not force and index_is_fresh(conn, repo_root) and has_existing and not stacktrace_files_missing:
        console.print("[dim]Index is up to date.[/]")
        return

    current_hash = get_repo_commit_hash(repo_root)
    incremental = has_existing and not force

    if incremental:
        console.print("[bold cyan]Updating RAG index incrementally...[/]")
        console.print("[dim]Checking which files changed since last index...[/]")
    else:
        console.print("[bold cyan]Building RAG index...[/]")
        console.print("[dim]This runs once per repo. Subsequent runs use the cache.[/]")
        conn.executescript("""
            DELETE FROM chunks;
            DELETE FROM chunk_embeddings;
            DELETE FROM meta;
            DROP TABLE IF EXISTS dir_keywords;
            DROP TABLE IF EXISTS dir_activity;
            DROP TABLE IF EXISTS file_versions;
        """)
        conn.commit()
        init_index_schema(conn)

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

    console.print(f"[dim]  {len(all_files)} indexable files found[/]")

    issue_kws = set(extract_keywords_from_issue(issue_text)) if issue_text else set()
    all_kws = {k.lower() for k in (issue_kws | set(skill_keywords))}

    if len(all_files) > max_index_files:
        console.print(f"[dim]  Pre-filtering to top {max_index_files} most relevant files...[/]")

        build_git_activity_map(repo_root, conn)
        build_dir_keyword_map(repo_root, conn)

        auto_dirs = get_guaranteed_dirs_from_map(
            issue_text, skill_keywords or [], conn, repo_root, top_n_dirs=12
        )

        guaranteed_files = []
        seen_guaranteed = set()
        for dir_path in auto_dirs:
            if not dir_path.exists():
                continue
            for f in dir_path.rglob("*"):
                try:
                    is_file = f.is_file()
                except (OSError, PermissionError):
                    continue
                rel_str = str(f.relative_to(repo_root)).replace("\\", "/")
                if (is_file
                        and f.suffix in INDEXABLE_EXTENSIONS
                        and f not in seen_guaranteed
                        and not any(
                            any(nd in p.lower() for nd in NOISE_DIRS)
                            for p in f.parts
                        )
                        and f.name != _TOOL_FILENAME
                        and not is_doc_file(rel_str)):
                    seen_guaranteed.add(f)
                    guaranteed_files.append(f)

        if guaranteed_files:
            console.print(
                f"[dim]  Auto-guaranteed {len(guaranteed_files)} files "
                f"from {len(auto_dirs)} directories[/]"
            )

        # Guarantee stacktrace frame files (frames 4+)
        if issue_text:
            frame_file_pairs = re.findall(
                r'#(\d+)\s+0x[0-9a-f]+[^\n]*\b([A-Za-z][\w]+\.(?:cpp|h|inc|c)):\d+',
                issue_text
            )
            if not frame_file_pairs:
                for line in issue_text.split("\n"):
                    fm = re.search(r'#(\d+)\s+0x[0-9a-f]+', line)
                    if fm:
                        fnames = re.findall(r'([A-Za-z][\w]+\.(?:cpp|h|inc|c)):\d+', line)
                        for fn in fnames:
                            frame_file_pairs.append((fm.group(1), fn))
            stacktrace_files = [
                fname for fnum, fname in frame_file_pairs if int(fnum) >= 4
            ]
            console.print(
                f"[dim]  Stacktrace frame/file pairs (>=4): "
                f"{[(n,f) for n,f in frame_file_pairs if int(n)>=4][:6]}[/]"
            )
            seen_stems: set[str] = set()
            filtered_st = []
            for f in stacktrace_files:
                stem = f.rsplit('.', 1)[0]
                if stem not in seen_stems:
                    seen_stems.add(stem)
                    filtered_st.append(f)
            stacktrace_files = filtered_st
            for fname in set(stacktrace_files):
                matches = list(repo_root.rglob(fname))
                for match in matches[:3]:
                    try:
                        rel_str = str(match.relative_to(repo_root)).replace("\\", "/")
                        if (match not in seen_guaranteed
                                and not is_doc_file(rel_str)
                                and match.suffix in INDEXABLE_EXTENSIONS):
                            seen_guaranteed.add(match)
                            guaranteed_files.append(match)
                    except (OSError, ValueError):
                        continue
            if stacktrace_files:
                console.print(
                    f"[dim]  Stacktrace-guaranteed files: "
                    f"{list(set(stacktrace_files))[:5]}[/]"
                )

        guaranteed_set = set(guaranteed_files)
        remaining = [f for f in all_files if f not in guaranteed_set]
        remaining_sorted = sorted(
            remaining, key=lambda f: score_file(f, repo_root, all_kws), reverse=True
        )
        slots_left = max(0, max_index_files - len(guaranteed_files))
        scored_files = [f for f in remaining_sorted[:slots_left]
                        if score_file(f, repo_root, all_kws) >= 0]

        all_files = guaranteed_files + scored_files

        console.print("[dim]  Top scored files:[/]")
        for f in remaining_sorted[:5]:
            rel = str(f.relative_to(repo_root)).replace("\\", "/")
            s = score_file(f, repo_root, all_kws)
            console.print(f"[dim]    {rel} (score={s})[/]")

    console.print(f"[dim]  Indexing {len(all_files)} files[/]")

    if incremental:
        new_files, changed_files = get_stale_files(conn, repo_root, all_files)
        files_to_process = new_files + changed_files
        if not files_to_process:
            console.print('[green]All indexed files are up to date.[/]')
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('commit_hash',?)", (current_hash,))
            conn.commit()
            return
        console.print(f'[dim]  {len(new_files)} new, {len(changed_files)} changed files[/]')
        for f in changed_files:
            rel = str(f.relative_to(repo_root)).replace("\\", "/")
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM chunks WHERE filepath=?", (rel,)
            ).fetchall()]
            if old_ids:
                ph = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM chunk_embeddings WHERE rowid IN ({ph})", old_ids)
                conn.execute("DELETE FROM chunks WHERE filepath=?", (rel,))
            conn.execute("DELETE FROM file_versions WHERE filepath=?", (rel,))
        conn.commit()
    else:
        files_to_process = all_files

    all_chunks = []
    for f in files_to_process:
        all_chunks.extend(chunk_file(f, repo_root))

    if all_chunks:
        console.print(f'[dim]  {len(all_chunks)} chunks to embed[/]')
    else:
        console.print('[dim]  No new chunks to embed.[/]')

    BATCH_SIZE   = 16
    COMMIT_EVERY = 100
    total_embedded = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    file_hashes = get_file_commit_hashes_batch(repo_root, files_to_process)

    for batch_start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[batch_start:batch_start + BATCH_SIZE]
        texts = [c["content"][:1000] for c in batch]
        embeddings = embed_texts(texts)

        for chunk, emb in zip(batch, embeddings):
            if not emb or len(emb) != EMBED_DIMENSIONS:
                continue
            cur = conn.execute(
                "INSERT INTO chunks (filepath, start_line, end_line, content) VALUES (?,?,?,?)",
                (chunk["filepath"], chunk["start_line"], chunk["end_line"], chunk["content"])
            )
            conn.execute(
                "INSERT INTO chunk_embeddings (rowid, embedding) VALUES (?,?)",
                (cur.lastrowid, vec_to_blob(emb))
            )
            total_embedded += 1

        if total_embedded % COMMIT_EVERY == 0:
            conn.commit()

        if all_chunks:
            pct = 100 * min(batch_start + BATCH_SIZE, len(all_chunks)) // len(all_chunks)
            console.print(
                f'[dim]  {total_embedded}/{len(all_chunks)} embedded ({pct}%)...[/]', end='\r'
            )

    conn.commit()

    for f in files_to_process:
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        conn.execute(
            "INSERT OR REPLACE INTO file_versions (filepath, commit_hash, indexed_at) VALUES (?, ?, ?)",
            (rel, file_hashes.get(rel, 'unknown'), now)
        )

    total_now = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # Rebuild FTS5 index from chunks table
    try:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    except Exception:
        pass  # FTS rebuild failed — keyword search will fall back to table scan
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('commit_hash',?)", (current_hash,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('total_chunks',?)", (str(total_now),))
    conn.commit()

    if incremental and total_embedded > 0:
        console.print(
            f'\n[green]Index updated: {total_embedded} chunks re-embedded, {total_now} total[/]'
        )
    elif not incremental:
        console.print(f'\n[green]Index built: {total_embedded} chunks embedded[/]')


# ── Query augmentation ─────────────────────────────────────────────────────────

def augment_query(issue_text: str, skill_keywords: list) -> tuple[str, list[str]]:
    """
    Extract high-signal technical identifiers from issue text.
    Returns (augmented_query, keywords_list).
    """
    identifiers = []

    first_line = issue_text.split('\n')[0]
    title_tokens = re.findall(r'\b([A-Za-z_]\w{3,})\b', first_line)
    identifiers.extend(title_tokens)

    backtick_contents = re.findall(r'`([^`]{2,300})`', issue_text)
    for bc in backtick_contents:
        if len(bc) <= 60 and ' ' not in bc:
            identifiers.append(bc)
        tokens = re.findall(r'\b([A-Za-z_]\w{2,})\b', bc)
        identifiers.extend(tokens)

    camel = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]*)+\w*)\b', issue_text)
    identifiers.extend(camel)

    macros = re.findall(r'\b([A-Z][A-Z0-9_]{3,})\b', issue_text)
    identifiers.extend(macros)

    func_calls = re.findall(r'\b(\w{4,})\s*\(', issue_text)
    identifiers.extend(func_calls)

    intrinsic_parts = []
    intrinsics = re.findall(r'@(llvm\.\w+(?:\.\w+)*)', issue_text)
    for intr in intrinsics:
        identifiers.append(intr)
        parts = intr.split('.')
        intrinsic_parts.extend(parts[1:])
    label_ids = re.findall(r'llvm:([a-z][a-z0-9]+)', issue_text.lower())
    intrinsic_parts.extend(label_ids)

    stacktrace_parts = []
    frame_fns = re.findall(
        r'#([0-9]+)\s+0x[0-9a-f]+\s+([\w:~<>*&,\s]+?)(?:\s*(?:\(|<))',
        issue_text
    )
    for frame_num_str, fn in frame_fns:
        if int(frame_num_str) < 4:
            continue
        fn = fn.strip()
        parts = fn.split('::')
        if len(parts) >= 2:
            stacktrace_parts.append(parts[-1].strip())
            if len(parts) >= 3:
                stacktrace_parts.append(f"{parts[-2]}::{parts[-1]}".strip())
        elif fn and len(fn) >= 5:
            stacktrace_parts.append(fn)

    frame_files = re.findall(r'\b([A-Za-z][\w]+\.(?:cpp|h|inc|c)):\d+', issue_text)
    stacktrace_parts.extend(frame_files)

    paths = re.findall(r'\b(\w+/\w+(?:/\w+)*\.\w+)\b', issue_text)
    identifiers.extend(paths)

    quoted = re.findall(r'"([^"]{5,60})"', issue_text)
    for q in quoted:
        tokens = re.findall(r'\b(\w{4,})\b', q)
        identifiers.extend(tokens)

    UNIVERSAL_STOP = {
        'repository', 'branch', 'version', 'tested', 'operating',
        'systems', 'linked', 'report', 'description', 'reproducer',
        'expected', 'actual', 'behavior', 'behaviour', 'output',
        'function', 'method', 'class', 'object', 'variable',
        'pointer', 'buffer', 'memory', 'address', 'offset',
        'error', 'result', 'value', 'return', 'nullptr', 'null',
    }

    def is_code_identifier(token: str) -> bool:
        if not token or token.isdigit():
            return False
        t = token.strip()
        tl = t.lower()
        if tl in UNIVERSAL_STOP:
            return False
        if len(t) < 5:
            return False
        if '/' in t or '\\' in t:
            return True
        if '_' in t:
            return True
        if re.search(r'[a-z][A-Z]', t):
            return True
        if t.isupper() and len(t) >= 5:
            return True
        if len(t) >= 11 and t.isalpha():
            return True
        return False

    seen = set()
    clean = []
    for ident in identifiers:
        norm = ident.strip()
        if norm and norm not in seen and is_code_identifier(norm):
            seen.add(norm)
            clean.append(norm)

    for part in intrinsic_parts:
        norm = part.strip().lower()
        if norm and len(norm) >= 3 and norm not in seen:
            seen.add(norm)
            clean.append(norm)

    for part in stacktrace_parts:
        norm = part.strip()
        if norm and len(norm) >= 4 and norm.lower() not in seen:
            seen.add(norm.lower())
            clean.append(norm)

    # Decompose CamelCase identifiers into component tokens so they can
    # match directory names in the keyword map.
    # SemaChecking -> sema, checking  (matches clang/lib/Sema dir)
    # VectorCombine -> vector, combine (matches Vectorize via prefix)
    # InstCombineAndOrXor -> inst, combine, instcombine (matches InstCombine dir)
    # Only decompose the first 20 identifiers to avoid query bloat — the
    # most specific identifiers appear earliest (title, backticks, stacktrace).
    decomposed = []
    for ident in list(clean)[:20]:
        parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', ident)
        for p in parts:
            pl = p.lower()
            if len(pl) >= 4 and pl not in seen:
                seen.add(pl)
                decomposed.append(pl)
    clean.extend(decomposed)

    keyword_prefix = " ".join(clean[:40])
    skill_prefix = " ".join(skill_keywords[:20])
    augmented = keyword_prefix + "\n" + skill_prefix + "\n" + issue_text[:1500]

    return augmented, clean


# ── Keyword search ─────────────────────────────────────────────────────────────

def keyword_search(keywords: list[str], conn: sqlite3.Connection,
                   top_n: int = 8) -> list[dict]:
    """Precision keyword search over chunk content with scoring."""
    if not keywords:
        return []

    code_kws = [k for k in keywords
                if ' ' not in k and 4 <= len(k) <= 60
                and re.match(r"^[\w.\-:@/]+$", k)]
    if not code_kws:
        return []

    kw_lower = [k.lower() for k in code_kws]
    path_tokens = set()
    for k in code_kws:
        path_tokens.add(k.lower())
        for part in re.split(r'[_\-]', k):
            if len(part) >= 3:
                path_tokens.add(part.lower())
        camel = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', k)
        for part in camel:
            if len(part) >= 3:
                path_tokens.add(part.lower())

    def_patterns = []
    for k in code_kws:
        kl = re.escape(k.lower())
        def_patterns.append(re.compile(
            rf'(?:^|\s)(?:def |static\s+\w+\s+|\w+\s+){kl}\s*[\({{]',
            re.MULTILINE | re.IGNORECASE
        ))
        def_patterns.append(re.compile(
            rf'^{kl}\s*[=(]', re.MULTILINE | re.IGNORECASE
        ))

    try:
        # Use FTS5 full-text index if available — sub-millisecond keyword search.
        # Falls back to full table scan if FTS table doesn't exist yet
        # (older indexes built before FTS support was added).
        fts_rows = None
        try:
            # Query FTS5 for any chunk matching any keyword
            fts_query = " OR ".join(f'"{k}"' for k in code_kws[:8])
            fts_rows = conn.execute(
                "SELECT c.id, c.filepath, c.start_line, c.end_line, c.content "
                "FROM chunks_fts f "
                "JOIN chunks c ON c.id = f.rowid "
                "WHERE chunks_fts MATCH ?",
                (fts_query,)
            ).fetchall()
        except Exception:
            fts_rows = None  # FTS table not available, fall through

        if fts_rows is not None:
            all_chunks = fts_rows
        else:
            # Fallback for pre-FTS indexes: two-pass scan.
            # Pass 1: load only (id, filepath) — cheap, no content.
            # Pass 2: load full content only for rows whose filepath matches
            #         a keyword token OR whose id is in a sampled set.
            # This avoids loading 170MB of content for all 114k chunks.
            fp_rows = conn.execute(
                "SELECT id, filepath FROM chunks"
            ).fetchall()
            # Candidate ids: filepath contains a keyword token
            candidate_ids = [
                row_id for row_id, fp in fp_rows
                if any(t in fp.lower().replace("\\", "/") for t in path_tokens)
            ]
            # Also sample ~5% of chunks to catch keyword hits in unmatched paths
            sample_ids = [row_id for row_id, _ in fp_rows if row_id % 20 == 0]
            fetch_ids = list(dict.fromkeys(candidate_ids + sample_ids))[:2000]
            if fetch_ids:
                ph = ",".join("?" * len(fetch_ids))
                all_chunks = conn.execute(
                    f"SELECT id, filepath, start_line, end_line, content "
                    f"FROM chunks WHERE id IN ({ph})",
                    fetch_ids
                ).fetchall()
            else:
                all_chunks = []
    except Exception:
        return []

    scored = []
    for row in all_chunks:
        chunk_id, filepath, start, end, content = row
        content_lower = content.lower()
        filepath_lower = filepath.lower().replace("\\", "/")

        kws_found = [k for k, kl in zip(code_kws, kw_lower) if kl in content_lower]
        base = len(kws_found)
        if base == 0:
            continue

        exact_bonus = sum(2 for k in kws_found if k in content)
        defn_bonus = 0
        for pat in def_patterns:
            if pat.search(content):
                defn_bonus += 4
                break

        path_bonus = 0
        fp_parts = set(re.split(r'[/\\._]', filepath_lower))
        if fp_parts & path_tokens:
            path_bonus = 3

        concentration = base ** 1.5
        final_score = concentration + exact_bonus + defn_bonus + path_bonus

        min_kws = 2 if len(code_kws) >= 3 else 1
        if base >= min_kws:
            scored.append({
                "filepath": filepath,
                "start_line": start,
                "end_line": end,
                "content": content,
                "distance": max(0.0, 1.0 - (final_score / (len(code_kws) * 10))),
                "keyword_hits": final_score,
                "source": "keyword",
            })

    scored.sort(key=lambda x: x["keyword_hits"], reverse=True)
    source_only = [c for c in scored if not is_doc_file(c["filepath"])]
    results = source_only if source_only else scored
    return results[:top_n]


# ── Hybrid retrieval ───────────────────────────────────────────────────────────

def retrieve_chunks_vector(query, conn, top_k=TOP_K_CHUNKS):
    """Pure vector similarity search."""
    query_emb = embed_single(query)
    if not query_emb:
        return []
    rows = conn.execute("""
        SELECT c.filepath, c.start_line, c.end_line, c.content, ce.distance
        FROM chunk_embeddings ce
        JOIN chunks c ON c.id = ce.rowid
        WHERE ce.embedding MATCH ? AND k = ?
        ORDER BY ce.distance
    """, (vec_to_blob(query_emb), top_k)).fetchall()
    return [{"filepath": r[0], "start_line": r[1], "end_line": r[2],
             "content": r[3], "distance": r[4], "retrieval": "vector",
             "keyword_hits": 0}
            for r in rows]


def retrieve_chunks(query, conn, top_k=TOP_K_CHUNKS):
    """Legacy wrapper used by eval harness. Pure vector search."""
    return retrieve_chunks_vector(query, conn, top_k)


def embed_files_on_demand(filepaths: list, repo_root, conn) -> int:
    """
    Embed a small set of specific files and add them to the existing index.
    Used for on-demand indexing when cited files are missing from retrieved chunks.
    Returns the number of new chunks embedded.
    """
    if not filepaths:
        return 0
    import datetime
    all_chunks = []
    for fp in filepaths:
        path = Path(fp) if Path(fp).is_absolute() else repo_root / fp
        if path.exists() and path.suffix in INDEXABLE_EXTENSIONS:
            # Remove any existing chunks for this file first
            rel = str(path.relative_to(repo_root)).replace("\\", "/")
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM chunks WHERE filepath=?", (rel,)
            ).fetchall()]
            if old_ids:
                ph = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM chunk_embeddings WHERE rowid IN ({ph})", old_ids)
                conn.execute("DELETE FROM chunks WHERE filepath=?", (rel,))
            all_chunks.extend(chunk_file(path, repo_root))

    if not all_chunks:
        return 0

    console.print(f"[dim]  On-demand embedding {len(all_chunks)} chunks "
                  f"from {len(filepaths)} file(s)...[/]")
    BATCH_SIZE = 16
    total = 0
    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i+BATCH_SIZE]
        embeddings = embed_texts([c["content"][:1000] for c in batch])
        for chunk, emb in zip(batch, embeddings):
            if not emb or len(emb) != EMBED_DIMENSIONS:
                continue
            cur = conn.execute(
                "INSERT INTO chunks (filepath, start_line, end_line, content) VALUES (?,?,?,?)",
                (chunk["filepath"], chunk["start_line"], chunk["end_line"], chunk["content"])
            )
            conn.execute(
                "INSERT INTO chunk_embeddings (rowid, embedding) VALUES (?,?)",
                (cur.lastrowid, vec_to_blob(emb))
            )
            total += 1
    conn.commit()

    # Rebuild FTS5 index if available
    try:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.commit()
    except Exception:
        pass

    console.print(f"[dim]  On-demand: {total} chunks embedded[/]")
    return total


def hybrid_retrieve(
    issue_text: str,
    skill_keywords: list[str],
    conn: sqlite3.Connection,
    top_k: int = TOP_K_CHUNKS,
) -> tuple[list[dict], list[str]]:
    """Combines vector + keyword search. Returns (chunks, extracted_keywords)."""
    augmented_query, extracted_kws = augment_query(issue_text, skill_keywords)

    if extracted_kws:
        console.print(
            f"[dim]Query augmented with {len(extracted_kws)} identifiers: "
            f"{', '.join(extracted_kws[:8])}"
            f"{'...' if len(extracted_kws) > 8 else ''}[/]"
        )
        specific_kw = max(extracted_kws, key=len)
        try:
            hit_count = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE content LIKE ?", (f"%{specific_kw}%",)
            ).fetchone()[0]
            console.print(f"[dim]  Index check: '{specific_kw}' appears in {hit_count} chunks[/]")
        except Exception:
            pass

    vector_chunks = retrieve_chunks_vector(augmented_query, conn, top_k=top_k)
    keyword_chunks = keyword_search(extracted_kws, conn, top_n=max(12, top_k // 2))

    if keyword_chunks:
        kw_files = sorted(set(c["filepath"] for c in keyword_chunks))
        top_scores = [(c["filepath"].split("/")[-1], f"{c['keyword_hits']:.1f}")
                      for c in keyword_chunks[:5]]
        console.print(
            f"[dim]Keyword search found {len(keyword_chunks)} chunks "
            f"from: {', '.join(kw_files[:3])}"
            f"{'...' if len(kw_files) > 3 else ''}[/]"
        )
        console.print(
            f"[dim]  Top keyword scores: "
            f"{', '.join(f'{f}={s}' for f, s in top_scores)}[/]"
        )

    # Force-include chunks from files named in stacktrace frames 4+.
    # These files are already in the index (guaranteed at build time) but
    # may not rank highly enough via vector or keyword search because other
    # files share many of the same identifiers.
    # This is general: any file named in a crash stacktrace is highly likely
    # to contain the crash site or its immediate caller.
    stacktrace_forced: list[dict] = []
    # Extract (frame_num, filename, line_number) — use line number to select
    # the chunk containing the exact crash location rather than LIMIT 3.
    frame_file_line_pairs = re.findall(
        r'#([0-9]+)\s+0x[0-9a-f]+[^\n]*\b([A-Za-z][\w]+\.(?:cpp|h|inc|c)):(\d+)',
        issue_text
    )
    # Group by filename → take the lowest frame number's line for each file
    forced: dict = {}  # basename → line_number
    for fnum, fname, lineno in frame_file_line_pairs:
        if int(fnum) >= 4 and fname not in forced:
            forced[fname] = int(lineno)

    if forced:
        console.print(f"[dim]  Stacktrace-forcing chunks from: {sorted(forced.keys())}[/]")
        for basename, lineno in forced.items():
            # Try to find the chunk containing the exact crash line
            rows = conn.execute(
                """SELECT c.filepath, c.start_line, c.end_line, c.content
                   FROM chunks c
                   WHERE c.filepath LIKE ?
                   AND c.start_line <= ? AND c.end_line >= ?
                   LIMIT 1""",
                (f'%{basename}', lineno, lineno)
            ).fetchall()
            if not rows:
                # Fall back to first chunk in the file
                rows = conn.execute(
                    """SELECT c.filepath, c.start_line, c.end_line, c.content
                       FROM chunks c
                       WHERE c.filepath LIKE ?
                       LIMIT 1""",
                    (f'%{basename}',)
                ).fetchall()
            for r in rows:
                stacktrace_forced.append({
                    "filepath": r[0], "start_line": r[1],
                    "end_line": r[2], "content": r[3],
                    "distance": 0.0, "retrieval": "stacktrace",
                    "keyword_hits": 0,
                })

    seen = set()
    merged = []

    # Stacktrace-forced chunks go first — highest priority
    for chunk in stacktrace_forced:
        key = (chunk["filepath"], chunk["start_line"])
        if key not in seen:
            seen.add(key)
            merged.append(chunk)

    for chunk in keyword_chunks:
        key = (chunk["filepath"], chunk["start_line"])
        if key not in seen:
            seen.add(key)
            chunk["retrieval"] = "keyword+vector" if any(
                (c["filepath"], c["start_line"]) == key for c in vector_chunks
            ) else "keyword"
            merged.append(chunk)

    for chunk in vector_chunks:
        key = (chunk["filepath"], chunk["start_line"])
        if key not in seen:
            seen.add(key)
            chunk["retrieval"] = "vector"
            merged.append(chunk)

    return merged[:int(top_k * 2)], extracted_kws


def format_retrieved_chunks(chunks):
    if not chunks:
        return "(no relevant chunks retrieved)"
    seen = set()
    unique = []
    for c in chunks:
        key = (c["filepath"], c["start_line"])
        if key not in seen:
            seen.add(key); unique.append(c)
    by_file = {}
    for c in unique:
        by_file.setdefault(c["filepath"], []).append(c)

    def file_score(fchunks):
        best_kw = max((c.get("keyword_hits", 0) for c in fchunks), default=0)
        best_sim = max((1 - c.get("distance", 1.0) for c in fchunks), default=0)
        fp = fchunks[0].get("filepath", "") if fchunks else ""
        doc_penalty = 0.1 if is_doc_file(fp) else 1.0
        return (best_kw * 10 + best_sim) * doc_penalty

    sorted_files = sorted(by_file.items(), key=lambda x: file_score(x[1]), reverse=True)

    total_chars = 0
    parts = []
    PER_FILE_CAP = int(MAX_CONTEXT_CHARS * 0.40)

    for filepath, fchunks in sorted_files:
        file_chars = 0
        file_parts = []
        for c in sorted(fchunks, key=lambda x: x["start_line"]):
            retrieval_tag = c.get("retrieval", "vector")
            kw_hits = c.get("keyword_hits", 0)
            tag_str = (f"[{retrieval_tag}, {kw_hits} kw hits]"
                       if kw_hits else f"[{retrieval_tag}]")
            block = "  // lines {}-{} sim={:.2f} {}\n{}".format(
                c["start_line"], c["end_line"],
                1 - c["distance"], tag_str, c["content"])
            if total_chars + len(block) > MAX_CONTEXT_CHARS:
                break
            if file_chars + len(block) > PER_FILE_CAP:
                break
            file_parts.append(block)
            total_chars += len(block)
            file_chars += len(block)
        if file_parts:
            parts.append("### {}\n{}".format(filepath, "\n\n".join(file_parts)))
    return "\n\n".join(parts)