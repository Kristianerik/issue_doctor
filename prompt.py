"""
prompt.py - Prompt building, skills loading, and crash detection.

Depends on: config.py
"""

import re
from pathlib import Path

from config import SKILLS_DIR

try:
    from rich.prompt import Prompt
except ImportError:
    class Prompt:
        @staticmethod
        def ask(msg, **kw): return input(msg + ": ")
from config import console


# ── Prompts ────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """\
You are a principal-level software engineer and debugging expert with 20+ years
of experience in LLVM, Linux kernel, GCC, CPython, and major web runtimes.

HALLUCINATION RULES:
1. FILE PATHS: Only cite paths that appear VERBATIM in the retrieved source
   section above. If a file you want to reference is not listed there, write
   [UNCERTAIN: likely in <dir> — not in retrieved source]. NEVER invent
   plausible-sounding variants of filenames (e.g. InstCombineCastUtils.cpp
   when only InstCombineCasts.cpp was retrieved).
2. FUNCTION NAMES: Only cite functions visible in the retrieved source.
   If uncertain: [UNCERTAIN: look for function named X]
3. COMMANDS: Every shell command must be syntactically correct and runnable.
4. CONFIDENCE: Label each hypothesis [HIGH], [MEDIUM], or [LOW CONFIDENCE].
5. LINE NUMBERS: Cite actual line numbers shown in the source.
6. DRAFT PATCH: Must modify lines that appear in the source. Write
   [NOT IN RETRIEVED SOURCE] if relevant code was not retrieved.

OUTPUT FORMAT:

## 1. Issue Summary
What breaks, under what conditions, observable symptom.

## 2. Root Cause Hypothesis
Specific mechanism — file, function, line region. Label confidence.

## 3. Affected Components
- Exact file path (or [UNCERTAIN])
- Specific function name
- Why implicated

## 4. Reproduction Checklist
Numbered, copy-pasteable, correct commands.

## 5. Investigation Steps
Surgical. Name exact tool/flag/function, what to look for, what it rules out.

## 6. Draft Patch
REVIEW BEFORE APPLYING — this is a starting point, not a final fix.
Unified diff format:
```diff
--- a/path/to/file
+++ b/path/to/file
@@ -LINE,COUNT +LINE,COUNT @@
 context
-removed
+added
```
Write [NOT IN RETRIEVED SOURCE] if relevant code is not present.

## 7. Verification Plan
Specific tests, locations, frameworks, assertions.

## 8. Related Issues / Prior Art
Named bug class, similar fixes, relevant engineers.

## 9. Confidence Assessment
Evaluate and state confidence for each dimension using ONLY these labels:

**Retrieval:** [HIGH | MEDIUM | LOW]
- HIGH: target file appears verbatim in retrieved source with relevant functions visible
- MEDIUM: target file retrieved but specific fix location not clearly visible
- LOW: file not in retrieved source, diagnosis based on training knowledge only

**Mechanism:** [HIGH | MEDIUM | LOW]
- HIGH: root cause confirmed by reading actual source lines
- MEDIUM: mechanism is plausible but not directly visible in retrieved source
- LOW: speculative based on issue description only

**Patch:** [HIGH | MEDIUM | LOW]
- HIGH: patch modifies lines visible in retrieved source, API calls verified
- MEDIUM: patch direction correct but line numbers approximate or API unverified
- LOW: patch is conceptual only, not grounded in retrieved source

**Recommended action:**
Based on the above, give ONE of:
- "Apply and test" — HIGH confidence across all three dimensions
- "Review logic before applying" — MEDIUM confidence in patch
- "Use as direction only — do not apply directly" — LOW confidence in patch
- "Retrieve [specific file] and re-diagnose" — file not in retrieved source
"""


CRASH_GUIDANCE = """# CRASH BUG — MANDATORY DIAGNOSIS RULES

This issue contains a stacktrace. You MUST follow these rules:

1. THE FIX IS IN THE COMPILER/PROGRAM, NEVER IN THE INPUT CODE.
   The input may be malformed, incomplete, or fuzzer-generated.
   Compilers and interpreters must handle all inputs without crashing.
   Do NOT suggest fixing the input code, removing imports, or correcting
   undefined types in the reproducer. The input is correct as-is for
   the purpose of this bug report.

2. FRAME NUMBERING RULE:
   - Frames 0-3 = signal handlers — IGNORE completely
   - Frame 4 = crash site (the function that dereferenced a bad pointer)
   - The fix location is in the CALLER that passed bad data — typically
     frame 5 or 6. Look for where the null/invalid value was created
     or retrieved, and add a null check BEFORE it is passed down.

3. PATCH RULE:
   Do not add null checks inside the crashing function (frame 4).
   Find the function that CREATED or RETRIEVED the bad value and
   add the check there — before passing it to the caller below.

"""


USER_TEMPLATE = """\
Diagnose the following GitHub issue and produce a draft patch.

Before writing anything:
1. Find the relevant functions in the RETRIEVED SOURCE CODE above.
2. Read their logic using the line numbers shown.
3. Identify the exact line(s) where the bug manifests.
4. Write diagnosis and patch based ONLY on what you see in the source.
5. If the fix location is not in the retrieved source, say [NOT IN RETRIEVED SOURCE].

---
{issue_text}
---
"""


# ── Crash detection ────────────────────────────────────────────────────────────

def is_crash_issue(issue_text: str) -> bool:
    """Detect whether an issue contains a stacktrace crash report."""
    return bool(re.search(
        r'#[0-9]+\s+0x[0-9a-f]+.*(SIGSEGV|segfault|crash|Signals\.inc|PrintStackTrace)',
        issue_text, re.IGNORECASE
    ) or re.search(r'#4\s+0x[0-9a-f]+', issue_text))


# ── System prompt builder ──────────────────────────────────────────────────────

def build_system_prompt(skills, repo_context, used_rag,
                        issue_text="", crash_context="", commit_context="",
                        related_issues_context="", test_coverage_context="",
                        godbolt_context=""):
    prompt = ""

    # Prepend crash guidance BEFORE retrieved source for crash issues.
    # This ensures the model reads the rules before seeing any code,
    # matching the original design intent.
    if issue_text and is_crash_issue(issue_text):
        prompt += CRASH_GUIDANCE + "\n"

    # Inject crash investigation results if available
    if crash_context:
        prompt += (
            "# CRASH INVESTIGATION\n"
            + "=" * 60 + "\n"
            + "Deterministic + LLM-guided analysis of crash frames.\n"
            + "Use this to ground your fix location hypothesis.\n"
            + "=" * 60 + "\n\n"
            + crash_context + "\n\n"
            + "=" * 60 + "\n\n"
        )

    # Inject commit history for retrieved files
    if commit_context:
        prompt += (
            "# PRIOR FIXES\n"
            + "=" * 60 + "\n"
            + "Recent bug-fix commits touching the retrieved files.\n"
            + "Use these as context for the fix pattern and location.\n"
            + "=" * 60 + "\n\n"
            + commit_context + "\n\n"
            + "=" * 60 + "\n\n"
        )

    # Inject related issues
    if related_issues_context:
        prompt += (
            "# RELATED ISSUES\n"
            + "=" * 60 + "\n"
            + "Closed issues mentioning the same functions.\n"
            + "=" * 60 + "\n\n"
            + related_issues_context + "\n\n"
            + "=" * 60 + "\n\n"
        )

    # Inject test coverage
    if test_coverage_context:
        prompt += (
            "# EXISTING TESTS\n"
            + "=" * 60 + "\n"
            + "Test files covering the retrieved source files.\n"
            + "=" * 60 + "\n\n"
            + test_coverage_context + "\n\n"
            + "=" * 60 + "\n\n"
        )

    # Inject Godbolt reproducer if available — put it early so the model
    # sees the exact failing code before reading the source
    if godbolt_context:
        prompt += (
            "# REPRODUCER\n"
            + "=" * 60 + "\n"
            + "Exact source code from the issue's Godbolt link.\n"
            + "=" * 60 + "\n\n"
            + godbolt_context + "\n\n"
            + "=" * 60 + "\n\n"
        )

    if repo_context:
        method = "semantic RAG" if used_rag else "keyword scan"
        prompt += (
            "# RETRIEVED SOURCE CODE\n"
            + "=" * 60 + "\n"
            + f"Retrieved via {method}. REAL FILES WITH REAL LINE NUMBERS.\n"
            "Ground your diagnosis and patch in this source only.\n"
            + "=" * 60 + "\n\n"
            + repo_context
            + "\n\n" + "=" * 60 + "\n"
            "END OF RETRIEVED SOURCE\n"
            + "=" * 60 + "\n\n"
        )
    prompt += BASE_SYSTEM_PROMPT
    if skills:
        prompt += "\n\n---\n\n# Expert Knowledge Base\n\n"
        prompt += "\n\n".join(skills)
    return prompt


# ── Skills ─────────────────────────────────────────────────────────────────────

def load_all_skills():
    if not SKILLS_DIR.exists():
        console.print(f"[yellow]Skills folder not found: {SKILLS_DIR}[/]")
        return {}
    skills = {}
    seen = set()
    for subdir in [SKILLS_DIR / "core", SKILLS_DIR / "user", SKILLS_DIR]:
        if not subdir.exists():
            continue
        for f in subdir.glob("*.md"):
            if f.stem == "TEMPLATE" or f in seen:
                continue
            seen.add(f)
            text = f.read_text(encoding="utf-8")
            kws = _parse_trigger_keywords(text)
            skills[f.stem] = (kws, text)
    if skills:
        console.print(f"[dim]Skills found: {', '.join(sorted(skills.keys()))}[/]")
    return skills


def _parse_trigger_keywords(content):
    m = re.search(r"## Trigger keywords\s*\n(.*?)(?:\n##|\Z)", content, re.DOTALL)
    if not m:
        return []
    return [kw.strip().lower() for kw in re.split(r"[,\n]+", m.group(1).strip()) if kw.strip()]


# ── Repo guards ───────────────────────────────────────────────────────────────
# Pattern-based structural checks for repo-specific skills.
#
# Two-tier keyword system for skills that span multiple repo types:
# - STRONG keywords: domain-specific enough to fire the skill in any repo
# - WEAK keywords: generic terms that only fire if the repo guard passes
#
# Example: linux-kernel skill on an LLVM repo
#   "kasan" (strong) → loads skill — LLVM issues can involve kernel sanitizers
#   "mutex" (weak)   → blocked — too generic, not meaningful without kernel repo

def _is_kernel_repo(repo_root: Path) -> bool:
    """Linux kernel: three co-distinctive top-level directories."""
    p = Path(repo_root)
    return (p / "kernel").is_dir() and (p / "mm").is_dir() and (p / "fs").is_dir()


def _is_web_repo(repo_root: Path) -> bool:
    """Web/JS repo: has package.json or node_modules at root."""
    p = Path(repo_root)
    return (p / "package.json").exists() or (p / "node_modules").is_dir()


def _is_cpython_repo(repo_root: Path) -> bool:
    """CPython: Python/, Objects/, and Include/ co-exist only in CPython."""
    p = Path(repo_root)
    return (p / "Python").is_dir() and (p / "Objects").is_dir() and (p / "Include").is_dir()


# Repo guard functions keyed by skill name
SKILL_REPO_GUARDS: dict = {
    "linux-kernel": _is_kernel_repo,
    "web-js":       _is_web_repo,
    "cpython":      _is_cpython_repo,
}

# Strong keywords that fire a skill regardless of repo structure.
# These are specific enough that their presence alone justifies loading the skill.
SKILL_STRONG_KEYWORDS: dict = {
    "linux-kernel": {
        "kmalloc", "kfree", "kzalloc", "vmalloc", "krealloc",
        "bug_on", "warn_on", "panic", "oops", "kasan", "ubsan",
        "lockdep", "rcu", "ebpf", "bpf", "slab", "softirq",
        "hardirq", "cgroup", "syzbot", "syzkaller", "kselftest",
    },
}


def auto_detect_skills(issue_text, all_skills, repo_root=None):
    """
    Load a skill only if the issue text contains enough evidence AND
    the repo guard (if any) passes.

    Rules:
    1. Any keyword >= 8 chars that matches fires the skill alone
       (e.g. 'instcombine', 'selectiondag', 'syzkaller' are unambiguous)
    2. Multi-word phrases fire alone — inherently specific
       (e.g. 'race condition', 'null deref', 'loop vectorization')
    3. Short keywords (< 8 chars) require 2+ matches to fire
       (prevents 'atomic', 'kernel', 'thread' false-positives in clang issues)
    4. Repo guard: if a skill has a guard function, it must return True
       for the skill to load regardless of keyword matches
    """
    text_lower = issue_text.lower()
    detected = []
    for name, (kws, _) in all_skills.items():
        # Two-tier repo guard:
        # 1. If repo passes the structural guard → allow all keywords (fast path)
        # 2. If repo fails or is unknown → only strong keywords can fire the skill
        guard = SKILL_REPO_GUARDS.get(name)
        repo_passes_guard = (
            guard is None or
            (repo_root is not None and guard(Path(repo_root)))
        )

        matched = [kw for kw in kws if kw in text_lower]
        if not matched:
            continue

        # If repo guard failed, only allow genuinely domain-specific keywords
        if not repo_passes_guard:
            skill_strong = SKILL_STRONG_KEYWORDS.get(name, set())
            strong_matched = [kw for kw in matched if kw in skill_strong]
            if not strong_matched:
                continue
            # Strong keyword matched — load the skill
            detected.append(name)
            continue

        # Repo guard passed (or skill has no guard) — standard keyword rules
        strong = [kw for kw in matched if len(kw) >= 8 or ' ' in kw]
        if strong:
            detected.append(name)
            continue
        if len(matched) >= 2:
            detected.append(name)
    return detected


def resolve_skills(issue_text, all_skills, forced, interactive, repo_root=None):
    detected = auto_detect_skills(issue_text, all_skills, repo_root=repo_root)
    final_names = list(dict.fromkeys((forced or []) + detected))
    valid = set(all_skills.keys())
    final_names = [n for n in final_names if n in valid]
    if interactive:
        console.print()
        console.print(f"[bold cyan]Auto-detected skills:[/] {', '.join(final_names) or 'none'}")
        available = sorted(all_skills.keys())
        if available:
            console.print(f"[dim]Available: {', '.join(available)}[/]")
            override = Prompt.ask(
                "Skills to load (comma-separated, Enter to accept, 'none' to skip)",
                default=",".join(final_names) if final_names else "none",
            )
            if override.strip().lower() == "none":
                final_names = []
            else:
                final_names = [s.strip() for s in override.split(",") if s.strip()]
                final_names = [n for n in final_names if n in valid]
    if final_names:
        console.print(f"[green]Loading skills:[/] {', '.join(final_names)}")
    return [all_skills[n][1] for n in final_names], final_names


def get_skill_keywords(all_skills, loaded_names):
    kws = []
    for name in loaded_names:
        if name in all_skills:
            kws.extend(all_skills[name][0])
    return kws