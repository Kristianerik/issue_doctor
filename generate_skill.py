#!/usr/bin/env python3
"""
generate_skill.py - Bootstrap a skill file for a new repo.

Usage:
  python generate_skill.py --repo /path/to/repo --output skills/user/myrepo.md
  python generate_skill.py --repo /path/to/repo --github owner/repo --output skills/user/myrepo.md

Requires Ollama running with the configured model.
GitHub token optional — skips issue title mining if absent.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────────

import os

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

try:
    sys.path.insert(0, str(Path(__file__).parent))
    import config as _config
    OLLAMA_HOST  = _config.OLLAMA_HOST
    OLLAMA_MODEL = _config.OLLAMA_MODEL
    GITHUB_TOKEN = _config.GITHUB_TOKEN
except Exception:
    pass  # Use environment variable defaults above


# ── Step 1: Trigger keywords ───────────────────────────────────────────────────

def get_trigger_keywords(repo_root: Path, github_slug: str = "") -> list[str]:
    keywords = set()

    # Repo name
    keywords.add(repo_root.name.lower())

    # Top-level directory names — skip non-meaningful dirs
    NOISE_DIRS = {
        '__pycache__', 'skills', 'build', 'dist', 'node_modules',
        '.git', '.github', '.venv', 'venv', 'env', '.env',
        'target', 'vendor', 'third_party', 'third-party', 'external',
        'cmake', 'cmake_build', 'out', 'output', 'bin', 'obj',
        'doc', 'docs', 'documentation', 'examples', 'example', 'samples',
        'assets', 'static', 'media', 'resources', 'locale', 'i18n',
        'tools', 'scripts', 'config', 'configs', 'contrib', 'deploy',
    }
    try:
        for d in repo_root.iterdir():
            if (d.is_dir()
                    and not d.name.startswith('.')
                    and d.name.lower() not in NOISE_DIRS):
                name = d.name.lower()
                if len(name) >= 5:
                    keywords.add(name)
    except OSError:
        pass

    # Words from README headings — keep only technical identifiers,
    # not common English words. CamelCase, hyphenated, or digit-containing
    # terms are specific enough to be useful trigger keywords.
    COMMON_ENGLISH = {
        'after', 'avoid', 'before', 'between', 'build', 'change', 'check',
        'class', 'code', 'content', 'create', 'data', 'define', 'docs',
        'documentation', 'each', 'example', 'file', 'first', 'follow',
        'from', 'functions', 'getting', 'given', 'have', 'help', 'here',
        'include', 'information', 'install', 'into', 'lazy', 'library',
        'license', 'list', 'make', 'module', 'more', 'mutates', 'need',
        'note', 'object', 'objects', 'only', 'other', 'output', 'overview',
        'platform', 'platforms', 'please', 'programs', 'project', 'quick',
        'race', 'read', 'readme', 'references', 'release', 'require',
        'returns', 'running', 'section', 'see', 'setup', 'should', 'simple',
        'some', 'source', 'start', 'started', 'support', 'system', 'that',
        'then', 'there', 'this', 'through', 'type', 'under', 'usage',
        'using', 'version', 'what', 'when', 'where', 'which', 'will',
        'with', 'work', 'write', 'written', 'your',
        'removal', 'adding', 'getting', 'running', 'using', 'building',
        'grammar', 'syntax', 'format', 'layout', 'config', 'settings',
        'release', 'changelog', 'history', 'license', 'authors',
    }
    for readme_name in ['README.md', 'README.rst', 'README.txt', 'README']:
        readme = repo_root / readme_name
        if readme.exists():
            try:
                text = readme.read_text(encoding='utf-8', errors='ignore')
                for m in re.finditer(r'^#{1,2}\s+(.+)', text, re.MULTILINE):
                    words = re.findall(r'\b([A-Za-z][A-Za-z0-9_\-]{2,})\b',
                                       m.group(1))
                    for w in words:
                        wl = w.lower()
                        # Keep: CamelCase, hyphenated, digit-containing,
                        # long (>8 chars), or medium (>5 chars) if not common English
                        if (wl not in COMMON_ENGLISH
                                and (re.search(r'[a-z][A-Z]', w)  # CamelCase
                                     or '-' in w                   # hyphenated
                                     or any(c.isdigit() for c in w)  # has digit
                                     or len(w) > 5)):              # not a common short word
                            keywords.add(wl)
            except OSError:
                pass
            break

    # Issue title keywords via GitHub API (if token available)
    if github_slug and GITHUB_TOKEN:
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {GITHUB_TOKEN}",
            }
            owner, repo = github_slug.split('/', 1)
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                headers=headers,
                params={"state": "all", "per_page": 50, "sort": "updated"},
                timeout=10,
            )
            if r.status_code == 200:
                titles = [i.get('title', '') for i in r.json()]
                word_counts: dict[str, int] = {}
                for title in titles:
                    for w in re.findall(r'\b([A-Za-z][A-Za-z0-9_\-]{3,})\b',
                                        title):
                        wl = w.lower()
                        word_counts[wl] = word_counts.get(wl, 0) + 1
                # Top identifiers — apply same quality filter as README headings
                top = sorted(word_counts.items(),
                             key=lambda x: x[1], reverse=True)[:60]
                for w, _ in top:
                    if (w not in COMMON_ENGLISH
                            and w not in noise
                            and (re.search(r'[a-z][A-Z]', w)
                                 or '-' in w
                                 or any(c.isdigit() for c in w)
                                 or len(w) > 5)):
                        keywords.add(w)
                    if len(keywords) >= 40:
                        break
        except Exception:
            pass

    # Filter noise
    noise = {'the', 'and', 'for', 'with', 'from', 'that', 'this',
             'not', 'are', 'was', 'has', 'fix', 'bug', 'issue'}
    return sorted(k for k in keywords if k not in noise and len(k) >= 3)[:40]


# ── Step 2: Key source locations ───────────────────────────────────────────────

def get_top_changed_files(repo_root: Path) -> dict[str, list[str]]:
    """
    Find top 20 most-changed source files in the last year, grouped by directory.
    Returns {dir: [file, file, ...]} for top 3 dirs.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--since=1 year ago", "--name-only",
             "--format=", "--diff-filter=AM"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=30,
        )
        if result.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}

    file_counts: dict[str, int] = {}
    source_exts = {'.c', '.cpp', '.cc', '.h', '.hpp', '.py', '.rs', '.go'}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if p.suffix in source_exts:
            file_counts[line] = file_counts.get(line, 0) + 1

    top_files = sorted(file_counts.items(),
                       key=lambda x: x[1], reverse=True)[:20]

    dir_files: dict[str, list[str]] = {}
    for filepath, _ in top_files:
        parts = filepath.replace('\\', '/').split('/')
        directory = '/'.join(parts[:-1]) if len(parts) > 1 else '.'
        dir_files.setdefault(directory, []).append(parts[-1])

    dir_counts = {d: sum(file_counts.get(f'{d}/{f}', 0)
                         for f in files)
                  for d, files in dir_files.items()}
    top_dirs = sorted(dir_counts.items(),
                      key=lambda x: x[1], reverse=True)[:3]

    return {d: dir_files[d][:3] for d, _ in top_dirs}


# ── Step 3: LLM overview + bug patterns ────────────────────────────────────────

def get_overview_and_patterns(repo_root: Path,
                               top_dirs: dict[str, list[str]]) -> tuple[str, str]:
    """
    Single Ollama call to generate overview and common bug patterns.
    Returns (overview_paragraph, bug_patterns_text).
    """
    # Gather README excerpt
    readme_excerpt = ""
    for readme_name in ['README.md', 'README.rst', 'README']:
        readme = repo_root / readme_name
        if readme.exists():
            try:
                readme_excerpt = readme.read_text(
                    encoding='utf-8', errors='ignore')[:1500]
            except OSError:
                pass
            break

    files_summary = "\n".join(
        f"  {d}/: {', '.join(files)}"
        for d, files in top_dirs.items()
    )

    prompt = f"""/no_think
You are writing a skill file for an AI debugging tool. Given this repo info:

README excerpt:
{readme_excerpt[:800]}

Most-changed source directories (past year):
{files_summary}

Write exactly:
1. A 2-sentence overview of what this codebase does and what kinds of bugs occur.
2. 2-3 common bug pattern names (just the names, one per line, prefixed with "- ").

Format:
OVERVIEW: <2 sentences>
PATTERNS:
- <pattern name>
- <pattern name>
"""

    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 400},
            },
            timeout=60,
        )
        if r.status_code != 200:
            return "", ""

        content = r.json().get("message", {}).get("content", "").strip()
        content = re.sub(r'<think>.*?</think>', '', content,
                         flags=re.DOTALL).strip()

        overview = ""
        patterns = ""
        ov_m = re.search(r'OVERVIEW:\s*(.+?)(?=PATTERNS:|$)',
                         content, re.DOTALL)
        pat_m = re.search(r'PATTERNS:\s*(.+)', content, re.DOTALL)
        if ov_m:
            overview = ov_m.group(1).strip()
        if pat_m:
            patterns = pat_m.group(1).strip()
        return overview, patterns

    except Exception:
        return "", ""


# ── Step 4: Test locations ─────────────────────────────────────────────────────

def get_test_locations(repo_root: Path) -> list[str]:
    """Find test directories at depth 1 and 2."""
    test_dirs = []
    try:
        for d in repo_root.iterdir():
            try:
                if not d.is_dir() or d.name.startswith('.'):
                    continue
            except (OSError, PermissionError):
                continue
            if 'test' in d.name.lower() and 'build' not in d.name.lower():
                test_dirs.append(d.name)
            try:
                for sub in d.iterdir():
                    try:
                        if (sub.is_dir() and 'test' in sub.name.lower()
                                and d.name.lower() != 'build'):
                            test_dirs.append(f"{d.name}/{sub.name}")
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass
    return test_dirs[:8]


# ── Step 5: Contributors ───────────────────────────────────────────────────────

def get_contributors(repo_root: Path, github_slug: str = "",
                     token: str = "") -> list[str]:
    # Try git shortlog first — actual local commit history
    try:
        result = subprocess.run(
            ["git", "shortlog", "-sn", "--since=1 year ago", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            contributors = []
            for line in result.stdout.strip().splitlines()[:5]:
                m = re.match(r'\s*\d+\s+(.+)', line)
                if m:
                    contributors.append(m.group(1).strip())
            if contributors:
                return contributors
    except Exception:
        pass

    # Fall back to GitHub API — returns actual GitHub handles
    # Useful for stale forks where local shortlog is empty or misleading
    gh_token = token or GITHUB_TOKEN
    if github_slug and gh_token:
        try:
            owner, repo = github_slug.split('/', 1)
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/contributors",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {gh_token}",
                },
                params={"per_page": 5},
                timeout=10,
            )
            if r.ok:
                return [f"@{c['login']}" for c in r.json()
                        if isinstance(c, dict) and 'login' in c]
        except Exception:
            pass

    return []


# ── Skill template renderer ────────────────────────────────────────────────────

def render_skill(repo_name: str, keywords: list[str], top_dirs: dict,
                 overview: str, bug_patterns: str,
                 test_dirs: list[str], contributors: list[str]) -> str:

    kw_line = ", ".join(keywords[:30])

    source_section = ""
    for directory, files in top_dirs.items():
        source_section += f"- `{directory}/` -- (most-changed directory)\n"
        for f in files:
            source_section += f"  - `{directory}/{f}`\n"

    if not overview:
        overview = f"(Generated from {repo_name} repo structure. Edit to add accurate description.)"

    patterns_section = ""
    if bug_patterns:
        for line in bug_patterns.splitlines():
            line = line.strip()
            if line.startswith('-'):
                name = line.lstrip('- ').strip()
                patterns_section += f"\n### {name}\n<!-- Describe this pattern. -->\n"
    if not patterns_section:
        patterns_section = "\n### (Add common bug patterns here)\n"

    test_section = ""
    for td in test_dirs:
        test_section += f"- `{td}/`\n"
    if not test_section:
        test_section = "- (no test directories detected)\n"

    contrib_section = ""
    for name in contributors:
        if name.startswith('@'):
            contrib_section += f"- `{name}` -- area of expertise\n"
        else:
            contrib_section += f"- `{name}` (git name -- GitHub handle may differ)\n"
    if not contrib_section:
        contrib_section = "- (run `git shortlog -sn` to find contributors)\n"

    return f"""# Skill: {repo_name}

## Trigger keywords
{kw_line}

## Overview
{overview}

## Key source locations
{source_section.rstrip()}

## Common bug patterns
{patterns_section.rstrip()}

## Investigation steps
1. Check the most-changed files listed above for recent fix patterns
2. Run `git log --oneline --since="6 months ago" -- <file>` on the relevant file
3. Look for related closed issues mentioning the same function or feature name

## Key test locations
{test_section.rstrip()}

## Subject matter experts
{contrib_section.rstrip()}
Note: git committer names may differ from GitHub handles.

## Useful commands
```bash
# Find recent fixes in a file
git log --oneline --since="6 months ago" --grep="fix" -- path/to/file

# Find all callers of a function
git grep -n "function_name(" -- "*.cpp" "*.h"
```
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap a skill file for a new repo."
    )
    parser.add_argument("--repo", required=True, metavar="PATH",
                        help="Path to the repo root")
    parser.add_argument("--output", required=True, metavar="FILE",
                        help="Output path for the skill file")
    parser.add_argument("--github", metavar="OWNER/REPO",
                        help="GitHub slug for issue title mining (optional)")
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    if not repo_root.exists():
        print(f"ERROR: repo path does not exist: {repo_root}", flush=True)
        sys.exit(1)

    print(f"Repo:   {repo_root}", flush=True)
    print(f"Model:  {OLLAMA_MODEL}", flush=True)
    print()

    print("Step 1/5: Extracting trigger keywords...", flush=True)
    keywords = get_trigger_keywords(repo_root, args.github or "")
    print(f"  {len(keywords)} keywords found", flush=True)

    print("Step 2/5: Finding most-changed source files...", flush=True)
    top_dirs = get_top_changed_files(repo_root)
    for d, files in top_dirs.items():
        print(f"  {d}/: {', '.join(files)}", flush=True)

    print("Step 3/5: Generating overview and bug patterns (LLM)...", flush=True)
    overview, patterns = get_overview_and_patterns(repo_root, top_dirs)
    if overview:
        print(f"  Overview: {overview[:80]}...", flush=True)
    else:
        print("  (LLM unavailable — placeholder inserted)", flush=True)

    print("Step 4/5: Finding test directories...", flush=True)
    test_dirs = get_test_locations(repo_root)
    print(f"  {test_dirs[:4]}", flush=True)

    print("Step 5/5: Finding contributors...", flush=True)
    contributors = get_contributors(repo_root, args.github or "", GITHUB_TOKEN)
    print(f"  {contributors[:3]}", flush=True)

    print()
    skill = render_skill(
        repo_name=repo_root.name,
        keywords=keywords,
        top_dirs=top_dirs,
        overview=overview,
        bug_patterns=patterns,
        test_dirs=test_dirs,
        contributors=contributors,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(skill, encoding='utf-8')
    print(f"Skill file written to: {output_path}", flush=True)
    print()
    print("Next steps:", flush=True)
    print("  1. Review and edit the generated skill file", flush=True)
    print("  2. Add specific function names and bug patterns from your experience", flush=True)
    print(f"  3. Test: python issue_doctor.py --url <url> --skills {output_path.stem}", flush=True)


if __name__ == "__main__":
    main()