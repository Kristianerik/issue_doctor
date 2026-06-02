# issue_doctor 🩺

Local AI-powered GitHub issue bug diagnosis. No data leaves your machine.

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) installed and running

```bash
pip install requests rich
```

## Setup

1. **Install Ollama**: https://ollama.com
2. **Pull a model** (pick one based on your RAM):

   | Model | RAM needed | Best for |
   |---|---|---|
   | `deepseek-r1:14b` | 10 GB | General code reasoning (recommended) |
   | `qwen2.5-coder:14b` | 10 GB | Code-heavy issues |
   | `deepseek-r1:32b` | 20 GB | Complex / multi-file issues |
   | `qwen2.5-coder:7b` | 6 GB | Lightweight / fast |

   ```bash
   ollama pull deepseek-r1:14b
   ```

3. **Pull an embedding model** (required for RAG):
   ```bash
   ollama pull nomic-embed-text
   ```

4. **Start Ollama** (if not already running):
   ```bash
   ollama serve
   ```

## First run

Drop `issue_doctor.py` into your repo and run it from there, or pass `--repo`
explicitly. The first run builds a RAG index over your codebase — this takes
several minutes for large repos (LLVM takes ~15–20 min). The index is cached
in `.issue_doctor/` and reused on every subsequent run.

```bash
# From inside the repo
cd /path/to/your/repo
python issue_doctor.py --url https://github.com/org/repo/issues/123

# Or with an explicit repo path
python issue_doctor.py --url https://github.com/org/repo/issues/123 --repo /path/to/repo
```

Check your setup before running a real issue:
```bash
python issue_doctor.py --diagnose
```

## Usage

```bash
# From a GitHub URL (public repo, no token needed)
python issue_doctor.py --url https://github.com/llvm/llvm-project/issues/199896

# From a private repo (set GITHUB_TOKEN)
GITHUB_TOKEN=ghp_xxx python issue_doctor.py --url https://github.com/org/repo/issues/42

# From a text file
python issue_doctor.py --text my_issue.txt

# Paste raw text directly
python issue_doctor.py --paste

# Override model or host
python issue_doctor.py --url <url> --model qwen2.5-coder:14b
python issue_doctor.py --url <url> --host http://192.168.1.10:11434

# Skip RAG entirely (faster, lower quality)
python issue_doctor.py --url <url> --no-repo
```

## Keeping the index current

After pulling new commits, update the index incrementally — only changed files
are re-embedded, so this is much faster than a full rebuild:

```bash
git pull
python issue_doctor.py --update --url <url>
```

To force a full rebuild from scratch (e.g. after switching branches or if the
index seems stale):

```bash
python issue_doctor.py --reindex --url <url>
```

The index lives in `.issue_doctor/embeddings.db` in the repo root. It is safe
to delete and will be rebuilt on the next run.

## All flags

### Input
| Flag | Description |
|---|---|
| `--url URL` | GitHub issue URL to fetch and diagnose |
| `--text TEXT` | Provide issue text directly |
| `--paste` | Read issue text from stdin |

### Index
| Flag | Description |
|---|---|
| `--reindex` | Full rebuild of the RAG index |
| `--update` | Incremental update — re-embeds only files changed since last index |
| `--max-files N` | Limit pre-filter to top N files (default: 3000) |

### Model / server
| Flag | Description |
|---|---|
| `--model MODEL` | Override the inference model (default: `deepseek-r1:14b`) |
| `--embed MODEL` | Override the embedding model (default: `nomic-embed-text`) |
| `--host HOST` | Override Ollama host (default: `http://localhost:11434`) |

### Repo / skills
| Flag | Description |
|---|---|
| `--repo PATH` | Repo path (default: auto-detected from current directory) |
| `--no-repo` | Disable RAG entirely |
| `--skills LIST` | Comma-separated skill names to load |
| `--no-skills` | Disable skills system entirely |

### Output
| Flag | Description |
|---|---|
| `--save-to FILE` | Save diagnosis to this file without prompting |
| `--no-save` | Skip the save prompt (for scripted use) |
| `--diagnose` | Run configuration check and exit |

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `deepseek-r1:14b` | Model to use |
| `GITHUB_TOKEN` | _(empty)_ | GitHub PAT for private repos / higher rate limits |

Set these in your shell profile or a `.env` file.

## Output

The tool produces a structured diagnosis with 8 sections:

1. **Issue Summary** — plain-English restatement
2. **Root Cause Hypothesis** — ranked technical candidates
3. **Affected Components** — files, modules, code paths
4. **Reproduction Checklist** — steps to reproduce locally
5. **Investigation Steps** — concrete debugging actions
6. **Proposed Fix** — code-level fix with trade-off analysis
7. **Verification Plan** — tests and regression checks
8. **Related Issues / Prior Art** — known bug class patterns

Each section ends with a **Confidence Assessment** using three levels:

| Level | Meaning |
|---|---|
| `[HIGH]` | Source was retrieved and read; claim is grounded in actual code |
| `[MEDIUM]` | Plausible based on issue text and general knowledge; not verified in source |
| `[LOW]` | Speculative; treat as a starting point for investigation only |

An **Automated Retrieval Warning** at the top of the report means the tool
could not retrieve the relevant source files. All claims in that report should
be treated as `[LOW]` regardless of what the individual sections say.

## Skills system

Skills are domain-specific knowledge files injected into the model's context
before diagnosis. They give the model a map of key files, common bug patterns,
and expert heuristics for a particular subsystem.

Skills are loaded automatically when their trigger keywords appear in the issue
title, body, or labels. You can also load them explicitly:

```bash
python issue_doctor.py --url <url> --skills clang-llvm,concurrency
```

### Built-in skills

| Skill | Triggers on |
|---|---|
| `clang-llvm` | clang, llvm, InstCombine, SelectionDAG, IR, codegen, vectorizer, ... |
| `concurrency` | race condition, deadlock, mutex, atomic, thread, TSan, ... |
| `linux-kernel` | kernel, mm, slab, RCU, oops, panic, syzbot, kasan, ... |
| `web-js` | javascript, typescript, react, node, promise, async, ... |

### Writing a custom skill

Copy `skills/user/TEMPLATE.md` to `skills/user/your-skill-name.md` and fill
it in. Skills are plain Markdown — name real files, real functions, and the
heuristics experienced engineers carry in their heads.

```bash
cp skills/user/TEMPLATE.md skills/user/my-subsystem.md
# edit it, then test:
python issue_doctor.py --url <url> --skills my-subsystem
```

The filename (without `.md`) is the skill name. User skills in `skills/user/`
take precedence over core skills with the same name.

## Dropping into a repo

`issue_doctor.py` has no project-specific dependencies. Copy it into any repo
and it works. Add to `.gitignore` if you don't want it committed:

```
issue_doctor.py
diagnosis_*.md
.issue_doctor/
```

## Privacy

All processing happens locally via Ollama. The only outbound network calls are:
- `api.github.com` — to fetch the issue (only when `--url` is used)
- `localhost:11434` — to your local Ollama instance

No issue content is sent to any cloud service.
