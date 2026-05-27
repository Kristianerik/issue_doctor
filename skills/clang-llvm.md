# Skill: Clang / LLVM Compiler Bugs

## Trigger keywords
clang, llvm, codegen, frontend, backend, IR, RISC-V, RVV, vectorizer,
loop vectorization, builtin, attribute, LTO, scalable vector, MLIR,
TableGen, SelectionDAG, InstCombine, SLP, CFG, BasicBlock, MachineInstr

## Key source locations

### Clang frontend
- `clang/lib/CodeGen/CGBuiltin.cpp`         — `__builtin_*` lowering including `__builtin_dynamic_object_size`
- `clang/lib/CodeGen/CGExpr.cpp`            — expression emission, MemberExpr vs DeclRefExpr handling
- `clang/lib/CodeGen/CGCall.cpp`            — call lowering, attribute propagation at call sites
- `clang/lib/Sema/SemaChecking.cpp`         — semantic checks for builtins and attributes
- `clang/lib/Analysis/MemoryBuiltins.cpp`   — object size analysis, counted_by resolution
- `clang/include/clang/AST/Attr.h`          — attribute definitions

### LLVM middle-end
- `llvm/lib/Transforms/InstCombine/`        — InstCombine passes, frequent source of attribute loss
- `llvm/lib/Transforms/Vectorize/LoopVectorize.cpp` — loop vectorizer
- `llvm/lib/Analysis/ValueTracking.cpp`     — known bits, object size queries
- `llvm/lib/IR/Instructions.cpp`            — IR instruction semantics

### LLVM backends
- `llvm/lib/Target/RISCV/`                  — RISC-V backend, RVV (vector extension) codegen
- `llvm/lib/Target/X86/`                    — x86/x86-64 backend

## Common bug patterns

### Attribute loss bugs
Attributes attached to IR values are silently dropped when:
- A value is cloned/rematerialised during inlining (`InlineFunction` in `llvm/lib/Transforms/Utils/InlineFunction.cpp`)
- InstCombine folds an instruction without copying metadata
- A `select` or `phi` merges two values with differing attributes

**Diagnostic**: compile with `-Xclang -disable-llvm-passes` to get raw IR before optimisation, then compare with `-O2` IR using `diff`.

### counted_by / object size bugs
`__builtin_dynamic_object_size` resolution walks the AST looking for a
`counted_by` annotation. The walk differs between:
- **Pointer path**: `p->fam` — arrives as `MemberExpr` through pointer, offset computed via `ObjectSizeOffsetEvaluator::visitMember`
- **Direct path**: `af.fam`  — arrives as `MemberExpr` on a `DeclRefExpr`, may fall through to a static layout calculation instead

Check `tryEvaluateBuiltinObjectSize` and `ObjectSizeOffsetEvaluator` in
`clang/lib/Analysis/MemoryBuiltins.cpp` for the divergence point.

### Scalable vector type confusion
`EVT::getVectorNumElements()` asserts/aborts on scalable vector types
(vscale × N elements). Always guard with `!VT.isScalableVector()` before
calling it. Common in loop vectoriser cost models and SelectionDAG legalisers.

### LTO-specific codegen bugs
With Full LTO the module is re-optimised after linking. Bugs that only
appear at `-flto` usually mean:
- An assumption valid per-TU breaks across TU boundaries
- An attribute or annotation is not serialised into bitcode (check `clang/lib/CodeGen/CGDebugInfo.cpp` and the bitcode writer)

## Useful debug flags
```bash
# Dump AST to see exact node types
clang -Xclang -ast-dump -fsyntax-only file.c

# Emit raw IR before any optimisation
clang -O2 -Xclang -disable-llvm-passes -emit-llvm -S -o raw.ll file.c

# Emit optimised IR
clang -O2 -emit-llvm -S -o opt.ll file.c

# Print IR after every pass (verbose)
clang -O2 -mllvm -print-after-all file.c 2>&1 | less

# RISC-V RVV specific
clang -march=rv64gcv -mrvv-vector-bits=zvl file.c -emit-llvm -S
```

## Subject matter experts (GitHub handles)
- `@bwendling`   — object size / counted_by
- `@nickdesaulniers` — clang frontend, kernel compat
- `@topperc`     — RISC-V backend
- `@fhahn`       — loop vectorizer
- `@arsenm`      — AMDGPU / generic IR

## Key test directories
- `clang/test/CodeGen/` — codegen regression tests
- `llvm/test/Transforms/` — optimisation pass tests
- `clang/test/Sema/` — semantic / attribute tests
