# Skill: Clang / LLVM Compiler Bugs

## Trigger keywords
clang, llvm, codegen, frontend, backend, IR, RISC-V, RVV, vectorizer,
loop vectorization, builtin, attribute, LTO, scalable vector, MLIR,
TableGen, SelectionDAG, InstCombine, SLP, CFG, BasicBlock, MachineInstr,
counted_by, object size, bdos, builtin_dynamic_object_size, flex array,
MemberExpr, DeclRefExpr, ObjectSizeOffsetEvaluator, MemoryBuiltins,
sema, Sema, SemaChecking, semantic, SemaDeclAttr, ExprConstant,
EvaluateForOverflow, CheckForIntOverflow, crash-on-invalid, crash-on-valid,
LValue, UnaryOperator, ASTContext, DiagnosticsEngine,
pragma clang module, module build, ForwardingDiagnosticConsumer,
TextDiagnostic, highlightLines, CheckPoint, createModuleFromSource

## Key source locations — VERIFIED REAL PATHS

### Sema — semantic analysis and expression evaluation
- `clang/lib/Sema/SemaChecking.cpp`
  - `CheckForIntOverflow()` — entry point for integer overflow checking
  - `EvaluateForOverflow()` — evaluates expressions for overflow; crash bugs
    often occur here when handling unusual AST nodes (UnaryOperator, etc.)
  - Fix pattern: add a guard for the specific AST node type before evaluating
- `clang/lib/Sema/SemaDecl.cpp` — declaration semantic checking
- `clang/lib/Sema/SemaExpr.cpp` — expression semantic checking
- `clang/lib/AST/ExprConstant.cpp`
  - `LValue::addUnsizedArray()` — crash site for invalid/negative array sizes;
    the FIX goes in the CALLER (SemaChecking.cpp), not here (see frame rule)
  - `EvaluateAsRValue()`, `EvaluateAsConstantExpr()` — constant folding entry points

### Object size / counted_by (this is where __builtin_dynamic_object_size lives)
- `clang/lib/Analysis/MemoryBuiltins.cpp`
  - `tryEvaluateBuiltinObjectSize()` — entry point for __bdos/__bos lowering
  - `ObjectSizeOffsetEvaluator::visitMember()` — handles MemberExpr
  - `ObjectSizeOffsetEvaluator::visitDeclRefExpr()` — handles direct variable refs
  - `ObjectSizeOffsetEvaluator::getCountedBySize()` — extracts counted_by value
- `clang/include/clang/Analysis/MemoryBuiltins.h` — declarations
- `clang/lib/CodeGen/CGBuiltin.cpp`
  - `CodeGenFunction::emitBuiltinObjectSize()` — lowers to IR after Sema
  - `CodeGenFunction::evaluateOrEmitBuiltinObjectSize()` — calls into MemoryBuiltins

### AST node types relevant to this bug
- `clang/include/clang/AST/Expr.h`
  - `MemberExpr` — `p->fam` (isArrow()==true) and `af.fam` (isArrow()==false)
  - `DeclRefExpr` — `af` in `af.fam`; the base of a non-pointer MemberExpr
- `clang/include/clang/AST/Decl.h`
  - `FieldDecl` — carries the `counted_by` attribute on the FAM field
  - `CountedByAttr` — the attribute itself

### Attribute definitions
- `clang/include/clang/Basic/Attr.td` — TableGen definition of CountedByAttr
- `clang/lib/Sema/SemaDeclAttr.cpp` — semantic validation of __counted_by

### Loop vectorizer / scalable vector bugs
- `llvm/lib/Transforms/Vectorize/LoopVectorize.cpp`
- `llvm/lib/CodeGen/SelectionDAG/SelectionDAGTargetInfo.cpp`
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` — RVV lowering

### LLVM middle-end (attribute/metadata loss)
- `llvm/lib/Transforms/Utils/InlineFunction.cpp`
- `llvm/lib/Transforms/InstCombine/InstCombineLoadStoreAlloca.cpp`
- `llvm/lib/Analysis/ValueTracking.cpp` — `computeObjectSize()`

### LTO
- `llvm/lib/LTO/LTO.cpp` — full LTO pipeline entry
- `llvm/lib/Bitcode/Writer/BitcodeWriter.cpp` — attribute serialisation into bc

### Diagnostic renderer
- `clang/lib/Frontend/TextDiagnostic.cpp` — `highlightLines()` does syntax
  highlighting using preprocessor checkpoints; crash here often means a
  parent/child CompilerInstance FileID mismatch (see below)
- `clang/lib/Frontend/TextDiagnosticPrinter.cpp` — constructs `TextDiagnostic`
  with a `Preprocessor*` at startup; that PP is reused for all diagnostics
  including those from child module compilations

## DO NOT HALLUCINATE THESE — they do not exist
- `clang/lib/Sema/ObjAttr.cpp` — does not exist
- `clang/lib/Analysis/ObjectSizeEvaluator.cpp` — does not exist
- `clang/lib/Analysis/ObjectSizeOffsetEvaluator.cpp` — does not exist
(The real file is clang/lib/Analysis/MemoryBuiltins.cpp for all of these)

## Common bug patterns

### Parent/child CompilerInstance — FileID mismatch
When `#pragma clang module build` is used, `createModuleFromSource` creates a
child `CompilerInstance` whose diagnostics are forwarded to the parent's
`TextDiagnosticPrinter` via `ForwardingDiagnosticConsumer`.

**Critical:** FileIDs are per-`SourceManager` and NOT globally unique. FID=1
in the parent may be `main.cpp`; FID=1 in the child is `B.map`. The parent's
`TextDiagnosticPrinter` holds the parent's `Preprocessor*`, so when
`highlightLines` calls `PP->getCheckPoint(child_FID, ...)` it looks up the
wrong buffer entirely.

Symptoms: assertion in `highlightLines` (`TextDiagnostic.cpp`) of the form
`CheckPoint >= Buff->getBufferStart() && CheckPoint <= Buff->getBufferEnd()`
only when `-fcolor-diagnostics` is active. Specifying `-std=c++XX` prevents
the crash because module compilation is skipped.

Stacktrace signature: `createModuleFromSource` or `HandlePragmaModuleBuild`
appear in the frames above the crash site. The crash site (diagnostic renderer)
and the fix site (parent/child PP boundary) are architecturally distant.

### Attribute loss during optimisation
Attributes on IR values are silently dropped when:
- A value is cloned during inlining (`InlineFunction`)
- InstCombine folds without copying `!annotation` metadata
- A `select`/`phi` merges values with differing attributes
Diagnostic: diff `raw.ll` vs `opt.ll`, search for missing `!annotation` nodes.

### Scalable vector type confusion
`EVT::getVectorNumElements()` hard-asserts on scalable vectors.
Guard pattern: `if (!VT.isScalableVector()) { ... getVectorNumElements() ... }`

### LTO-only crashes
Module re-optimised post-link. Causes:
1. Per-TU assumption breaks cross-TU (inlining exposes UB hidden by ABI boundary)
2. Attribute/annotation not serialised into bitcode

## Patch completeness requirements
A draft patch is NOT a mergeable patch. Every LLVM/Clang PR must include:

### Tests are mandatory
- Every fix needs a test that exercises the fixed code path
- Reviewers WILL request tests — flag this in the diagnosis, not after PR submission

### Test locations by fix type
- `clang/test/CodeGen/` — codegen regression tests (IR output checks)
- `clang/test/Analysis/` — static analysis tests
- `clang/test/Sema/` — semantic / attribute validation, error message checks
- `clang/test/Frontend/` — frontend pipeline tests
- `clang/unittests/Frontend/` — C++ unit tests for frontend internals
- `llvm/test/Transforms/` — optimisation pass tests (FileCheck on .ll input)

### Test format
- `.c`/`.cpp` input with `// RUN:` lines invoking clang, checked with FileCheck
- For crash bugs: test must trigger crash on unfixed code and pass on fixed code

### Fuzzer-triggered bugs — extra scrutiny required
- Before writing a fix, verify the bug is reachable in normal usage
- Ask: "Can a caller ever pass NULL/invalid input here in non-fuzzer usage?"
- If no: the fix may be unnecessary; a test proving unreachability is more
  valuable than a defensive null check

### Frontend crash fixes (CompilerInstance, PreprocessorOptions)
- Check all callers before adding a null guard
- If callers guarantee non-null by construction, the null check is dead code

## Useful debug commands
```bash
# Dump AST
clang -Xclang -ast-dump -fsyntax-only test.c 2>&1 | grep -A3 "MemberExpr\|DeclRefExpr"

# Raw vs optimised IR comparison
clang -O2 -Xclang -disable-llvm-passes -emit-llvm -S -o raw.ll test.c
clang -O2 -emit-llvm -S -o opt.ll test.c

# Trigger diagnostic renderer bugs (required for highlightLines crashes)
clang -fcolor-diagnostics test.cpp
```

## Crash issues
See crash diagnosis rules in the system prompt (prepended automatically for crash issues).
Key files: `clang/lib/Frontend/CompilerInstance.cpp`, `clang/lib/AST/ExprConstant.cpp`,
`clang/lib/Frontend/TextDiagnostic.cpp` (for module build / diagnostic renderer crashes)