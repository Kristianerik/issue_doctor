#!/usr/bin/env python3
"""
quality_eval.py - Measure issue_doctor output quality against ground truth.

Runs issue_doctor on each ground truth case and checks whether the diagnosis
correctly identifies the fix file and function.

Usage:
  python quality_eval.py --repo /path/to/llvm-project
  python quality_eval.py --repo /path/to/llvm-project --tool /path/to/issue_doctor.py
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

# ── Ground truth cases ────────────────────────────────────────────────────────

GROUND_TRUTH = [
    {
        "issue_number": 199506,
        "issue_url": "https://github.com/llvm/llvm-project/issues/199506",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineAndOrXor.cpp"],
        "fix_functions": ["foldBitmaskMul", "matchBitmaskMul"],
        "description": "[InstCombine] Fix type mismatch in foldBitmaskMul",
    },
    {
        "issue_number": 198389,
        "issue_url": "https://github.com/llvm/llvm-project/issues/198389",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineCompares.cpp"],
        "fix_functions": ["visitFCmpInst", "foldFCmpWithIntCast"],
        "description": "[InstCombine] Do not crash in compare of bitcast pattern",
    },
    {
        "issue_number": 199401,
        "issue_url": "https://github.com/llvm/llvm-project/issues/199401",
        "fix_files": ["llvm/lib/Transforms/InstCombine/InstCombineCalls.cpp"],
        "fix_functions": ["visitCallInst", "foldReductionIdiom"],
        "description": "[InstCombine] Fix vector_reduce_mul(sext <n x i1>) for odd n",
    },
    {
        "issue_number": 170072,
        "issue_url": "https://github.com/llvm/llvm-project/issues/170072",
        "fix_files": ["clang/lib/Sema/SemaChecking.cpp"],
        "fix_functions": ["CheckForIntOverflow", "EvaluateForOverflow"],
        "description": "[Clang][Sema] Fix crash EvaluateForOverflow for UnaryOperator",
    },
    {
        "issue_number": 200263,
        "issue_url": "https://github.com/llvm/llvm-project/issues/200263",
        "fix_files": ["llvm/lib/Transforms/Vectorize/VectorCombine.cpp"],
        "fix_functions": ["scalarizeLoad", "runImpl"],
        "description": "[VectorCombine] Don't scalarize atomic loads",
    },
]

# ── Diagnosis runner ──────────────────────────────────────────────────────────

def run_diagnosis(url: str, repo_root: str, tool_path: str) -> str:
    """Run issue_doctor and return the diagnosis text via temp file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                     delete=False, encoding='utf-8') as tf:
        tmp_path = tf.name

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"

    try:
        result = subprocess.run(
            ["python", tool_path, "--url", url,
             "--repo", repo_root, "--no-skills", "--save-to", tmp_path],
            capture_output=True, text=True, timeout=900,
            encoding="utf-8", errors="replace", env=env,
        )
        if result.returncode != 0:
            print(f"    [STDERR] {result.stderr[:300]}")
            print(f"    [STDOUT] {result.stdout[:200]}")
        p = Path(tmp_path)
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            p.unlink(missing_ok=True)
            return text
    except subprocess.TimeoutExpired:
        print("    [TIMEOUT]")
    except Exception as e:
        print(f"    [ERROR] {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return ""


# ── Scoring ───────────────────────────────────────────────────────────────────

def extract_cited_files(text: str) -> set:
    cited = set()
    for pat in [
        r"`([^`\n]{3,80}\.[ch](?:pp?)?)`",
        r"--- a/([^\n]+\.[ch](?:pp?)?)",
        r"\*\*(?:File|File Path|Exact file path)[:\s*]+`?([^\n`\s]+\.[ch](?:pp?)?)`?",
    ]:
        cited.update(re.findall(pat, text, re.IGNORECASE))
    return {c.strip() for c in cited if c.strip()}


def extract_cited_functions(text: str) -> set:
    cited = set()
    for pat in [
        r"\*\*(?:Function|Function Name|Specific Function)[:\s*]+`?([^\n`\s(]+)",
        r"`([a-zA-Z_][\w:]{3,}(?:::\w+)?)\(\)`",
    ]:
        cited.update(re.findall(pat, text, re.IGNORECASE))
    return {c.strip("` ") for c in cited
            if c.strip() and len(c.strip()) >= 4
            and not any(c.strip().endswith(e) for e in ('.cpp', '.c', '.h'))}


def score(text: str, case: dict) -> dict:
    if not text:
        return {"file_exact": False, "file_match": False,
                "function_match": False, "confidence_honest": None,
                "warning_fired": False, "cited_files": [], "cited_functions": []}

    cited_files = extract_cited_files(text)
    cited_fns = extract_cited_functions(text)
    fix_basenames = {f.split("/")[-1] for f in case["fix_files"]}

    file_exact = any(
        any(ff.split("/")[-1] in cf or cf.endswith(ff.split("/")[-1])
            for cf in cited_files)
        for ff in case["fix_files"]
    )
    file_match = bool({c.split("/")[-1] for c in cited_files} & fix_basenames)

    function_match = any(
        any(fn.lower() in cf.lower() or cf.lower() in fn.lower()
            for cf in cited_fns)
        for fn in case.get("fix_functions", [])
    )

    warning_fired = "Automated Retrieval Warning" in text
    self_high = ("[HIGH]" in text or "Apply and test" in text)
    confidence_honest = not (warning_fired and self_high)

    return {
        "file_exact": file_exact, "file_match": file_match,
        "function_match": function_match,
        "confidence_honest": confidence_honest,
        "warning_fired": warning_fired,
        "cited_files": list(cited_files)[:5],
        "cited_functions": list(cited_fns)[:5],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Issue Doctor output quality eval")
    parser.add_argument("--repo", required=True, metavar="PATH",
                        help="Path to the target repo")
    parser.add_argument("--tool", metavar="PATH",
                        help="Path to issue_doctor.py (default: same dir)")
    parser.add_argument("--cases", metavar="NUMBERS",
                        help="Comma-separated issue numbers to test (default: all)")
    parser.add_argument("--output", metavar="FILE",
                        help="Save JSON results to this file")
    args = parser.parse_args()

    repo_root = str(Path(args.repo).resolve())

    # Find issue_doctor.py
    if args.tool:
        tool_path = args.tool
    else:
        candidates = [
            Path(__file__).parent / "issue_doctor.py",
            Path(repo_root) / "issue_doctor.py",
        ]
        tool_path = next((str(p) for p in candidates if p.exists()), None)
        if not tool_path:
            print("ERROR: issue_doctor.py not found. Use --tool to specify path.")
            return

    print(f"Tool:  {tool_path}")
    print(f"Repo:  {repo_root}")
    print()

    # Filter cases
    cases = GROUND_TRUTH
    if args.cases:
        nums = {int(n.strip()) for n in args.cases.split(",")}
        cases = [c for c in cases if c["issue_number"] in nums]

    results = []
    for case in cases:
        n = case["issue_number"]
        print(f"Case #{n}: {case['description']}")
        print(f"  URL: {case['issue_url']}")

        t0 = time.time()
        text = run_diagnosis(case["issue_url"], repo_root, tool_path)
        elapsed = time.time() - t0

        if not text:
            print(f"  [FAILED — no diagnosis output]")
            results.append({"issue_number": n, "scores": None})
            continue

        s = score(text, case)
        f_mark = "OK" if s["file_exact"] else ("~~" if s["file_match"] else "XX")
        fn_mark = "OK" if s["function_match"] else "XX"
        c_mark = "OK" if s["confidence_honest"] else "!!"

        print(f"  File: {f_mark}  Function: {fn_mark}  Confidence honest: {c_mark}  "
              f"Warning: {'fired' if s['warning_fired'] else 'silent'}  "
              f"({elapsed:.0f}s)")
        if s["cited_files"]:
            print(f"  Cited files: {s['cited_files'][:2]}")
        if not s["file_exact"]:
            print(f"  Expected:    {case['fix_files'][0]}")
        print()

        results.append({
            "issue_number": n,
            "description": case["description"],
            "fix_files": case["fix_files"],
            "scores": s,
        })

    # Summary
    scored = [r for r in results if r.get("scores")]
    n = len(scored)
    if n:
        f_hits  = sum(1 for r in scored if r["scores"]["file_exact"])
        fn_hits = sum(1 for r in scored if r["scores"]["function_match"])
        honest  = sum(1 for r in scored if r["scores"]["confidence_honest"])
        print("=" * 50)
        print("Output Quality Summary")
        print("=" * 50)
        print(f"  Cases evaluated:     {n}")
        print(f"  File exact match:    {f_hits}/{n} ({100*f_hits//n}%)")
        print(f"  Function match:      {fn_hits}/{n} ({100*fn_hits//n}%)")
        print(f"  Confidence honest:   {honest}/{n} ({100*honest//n}%)")

    if args.output:
        out = args.output
    else:
        out = f"quality_results_{int(time.time())}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out}")


if __name__ == "__main__":
    main()