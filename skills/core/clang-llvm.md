# Skill: Clang / LLVM Compiler Bugs

## Trigger keywords
clang, llvm, codegen, frontend, backend, IR, RISC-V, RVV, vectorizer,
loop vectorization, builtin, attribute, LTO, scalable vector, MLIR,
TableGen, SelectionDAG, InstCombine, SLP, CFG, BasicBlock, MachineInstr,
counted_by, object size, bdos, builtin_dynamic_object_size, flex array,
MemberExpr, DeclRefExpr, ObjectSizeOffsetEvaluator, MemoryBuiltins,
sema, Sema, SemaChecking, semantic, SemaDeclAttr, ExprConstant,
EvaluateForOverflow, CheckForIntOverflow, crash-on-invalid, crash-on-valid,
LValue, UnaryOperator, ASTContext, DiagnosticsEngine

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
  - `ObjectSizeOffsetEvaluator::visitMember()` — handles MemberExpr; THIS is where
    the pointer-vs-direct divergence occurs. Pointer path walks counted_by;
    direct DeclRefExpr path falls through to static layout size.
  - `ObjectSizeOffsetEvaluator::visitDeclRefExpr()` — handles direct variable refs;
    does NOT currently check counted_by on the referenced decl
  - `ObjectSizeOffsetEvaluator::getCountedBySize()` — extracts counted_by value;
    only called from the pointer path today
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
  (NOT ObjAttr.cpp — that file does not exist)

### Loop vectorizer / scalable vector bugs
- `llvm/lib/Transforms/Vectorize/LoopVectorize.cpp`
  - Search for `getVectorNumElements` — all call sites must guard with
    `!VT.isScalableVector()` before calling
- `llvm/lib/CodeGen/SelectionDAG/SelectionDAGTargetInfo.cpp`
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` — RVV lowering

### LLVM middle-end (attribute/metadata loss)
- `llvm/lib/Transforms/Utils/InlineFunction.cpp` — cloning drops metadata here
- `llvm/lib/Transforms/InstCombine/InstCombineLoadStoreAlloca.cpp`
- `llvm/lib/Analysis/ValueTracking.cpp` — `computeObjectSize()`

### LTO
- `llvm/lib/LTO/LTO.cpp` — full LTO pipeline entry
- `llvm/lib/Bitcode/Writer/BitcodeWriter.cpp` — attribute serialisation into bc

## DO NOT HALLUCINATE THESE — they do not exist
- `clang/lib/Sema/ObjAttr.cpp` — does not exist
- `clang/lib/Analysis/ObjectSizeEvaluator.cpp` — does not exist
- `clang/lib/Analysis/ObjectSizeOffsetEvaluator.cpp` — does not exist
(The real file is clang/lib/Analysis/MemoryBuiltins.cpp for all of these)

## Exact diagnosis: counted_by on non-dereferenced objects

The divergence in `MemoryBuiltins.cpp` works like this:

```
__builtin_dynamic_object_size(p->fam, 1)   ← MemberExpr, isArrow()==true
  → visitMember() sees pointer base
  → calls getCountedBySize()  ✓  correct

__builtin_dynamic_object_size(af.fam, 1)   ← MemberExpr, isArrow()==false
  → visitMember() sees DeclRefExpr base (not a pointer dereref)
  → falls through to visitDeclRefExpr()
  → visitDeclRefExpr() returns static sizeof(af) or trailing-padding size  ✗ wrong
```

The fix: in `visitMember()` (or `visitDeclRefExpr()`), when the field has a
`CountedByAttr`, call `getCountedBySize()` regardless of whether the base is
a pointer. The base object (the struct instance) is accessible via the
`DeclRefExpr`; its `count` field can be emitted as an lvalue load.

## Useful debug commands (copy-pasteable)

```bash
# Dump AST to confirm MemberExpr node types
clang -Xclang -ast-dump -fsyntax-only test.c 2>&1 | grep -A3 "MemberExpr\|DeclRefExpr"

# Emit raw IR before any optimisation passes
clang -O2 -Xclang -disable-llvm-passes -emit-llvm -S -o raw.ll test.c

# Emit optimised IR for comparison
clang -O2 -emit-llvm -S -o opt.ll test.c

# Correct reproduce command for this bug
clang -fsigned-char -fno-strict-aliasing -O1 test.c -o test && ./test

# RISC-V RVV scalable vector
clang -march=rv64gcv -mrvv-vector-bits=zvl128b -emit-llvm -S test.c
```

## Common bug patterns

### Attribute loss during optimisation
Attributes on IR values are silently dropped when:
- A value is cloned during inlining (`InlineFunction`)
- InstCombine folds without copying `!annotation` metadata
- A `select`/`phi` merges values with differing attributes
Diagnostic: diff `raw.ll` vs `opt.ll`, search for missing `!annotation` nodes.

### Scalable vector type confusion
`EVT::getVectorNumElements()` hard-asserts on scalable vectors.
Guard pattern: `if (!VT.isScalableVector()) { ... getVectorNumElements() ... }`
Appears in loop vectoriser cost models and SelectionDAG legalisers.

### LTO-only crashes
Module re-optimised post-link. Causes:
1. Per-TU assumption breaks cross-TU (inlining exposes UB hidden by ABI boundary)
2. Attribute/annotation not serialised into bitcode → lost at link time

## Key test directories
- `clang/test/CodeGen/` — codegen regression tests (add new test here for bdos fix)
- `clang/test/Analysis/` — static analysis tests
- `clang/test/Sema/` — semantic / attribute validation tests
- `llvm/test/Transforms/` — optimisation pass tests

## Subject matter experts (GitHub handles)
- `@bwendling`      — object size, counted_by (primary contact for this bug)
- `@nickdesaulniers` — clang frontend, kernel compatibility
- `@topperc`        — RISC-V backend, RVV
- `@fhahn`          — loop vectorizer
- `@arsenm`         — generic IR, AMDGPU


## Crash issues

### crash-on-invalid pattern
For `crash-on-invalid` bugs, the preferred fix location is where the invalid
code should have been **rejected earlier**, not where the crash fires.

Before adding a null check or guard at the assertion/crash site:
1. Ask: is this code path reachable through **valid** code?
2. If yes: a guard at the crash site is appropriate
3. If no (only reachable via invalid input): find the earlier point where the
   invalid code should have been rejected and fix it there instead

Reviewers will ask this question before accepting a defensive null check.
A guard at the assertion is a last resort — the real fix is an earlier bailout.

See crash diagnosis rules in the system prompt (prepended automatically for crash issues).
Key files: `clang/lib/Frontend/CompilerInstance.cpp`, `clang/lib/AST/ExprConstant.cpp`