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

3. **Start Ollama** (if not already running):
   ```bash
   ollama serve
   ```

## Usage

```bash
# Interactive — prompts you for input method
python issue_doctor.py

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
```

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

You will be prompted to save the diagnosis to a `.md` file after each run.

## Dropping into a repo

Just copy `issue_doctor.py` into any repo. It has no project-specific
dependencies — everything is configured via environment variables or CLI flags.

Add to `.gitignore` if you don't want it committed:
```
issue_doctor.py
diagnosis_*.md
```

## Privacy

All processing happens locally via Ollama. The only outbound network calls are:
- `api.github.com` — to fetch the issue (only when `--url` is used)
- `localhost:11434` — to your local Ollama instance

No issue content is sent to any cloud service.
