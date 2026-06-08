# Skill: CPython Interpreter Bugs

## Trigger keywords
cpython, python, interpreter, bytecode, GIL, gil, reference counting, refcount,
Py_INCREF, Py_DECREF, PyObject, PyErr, import, importlib, frozen, sys.modules,
segfault, crash, assertion, Py_TPFLAGS, tp_dealloc, tp_new, tp_free,
PyUnicode, PyBytes, PyList, PyDict, PyTuple, PyLong, PyFloat,
typeobject, abstract, ceval, compile, symtable, ast, marshal,
pickle, struct, ctypes, socket, ssl, hashlib, weakref,
null byte, embedded null, PyUnicode_AsUTF8, PyUnicode_AsUTF8AndSize,
find_frozen, look_up_frozen, _PyImport, PyImport

## Key source locations — VERIFIED REAL PATHS

### Core interpreter
- `Python/ceval.c` — bytecode evaluation loop; crashes here are often in opcode
  dispatch or frame management
- `Python/compile.c` — AST to bytecode compiler
- `Python/symtable.c` — symbol table construction
- `Python/import.c`
  - `find_frozen()` — frozen module lookup; uses `PyUnicode_AsUTF8` which
    truncates at null bytes; fix: use `PyUnicode_AsUTF8AndSize`
  - `look_up_frozen()` — lower-level frozen table search
  - `_PyImport_FindExtensionObject()` — extension module cache lookup
- `Python/marshal.c` — .pyc file reading/writing
- `Python/pystate.c` — interpreter and thread state management

### Object system
- `Objects/typeobject.c` — type machinery, `tp_*` slot dispatch, MRO
- `Objects/object.c` — `PyObject_*` generic operations, reference counting
- `Objects/unicodeobject.c`
  - `PyUnicode_AsUTF8()` — returns `\0`-terminated pointer, truncates at
    embedded null bytes
  - `PyUnicode_AsUTF8AndSize()` — correct alternative; returns length too
- `Objects/longobject.c` — integer implementation
- `Objects/dictobject.c` — dict implementation
- `Objects/listobject.c` — list implementation
- `Objects/weakrefobject.c` — weak reference machinery

### Include headers
- `Include/cpython/object.h` — `PyObject`, `PyTypeObject` definitions
- `Include/internal/pycore_interp.h` — interpreter state internals
- `Include/internal/pycore_pystate.h` — thread state internals
- `Include/internal/pycore_import.h` — import system internals
- `Include/internal/pycore_unicodeobject.h` — unicode internals

### Modules
- `Modules/_io/` — I/O module implementation
- `Modules/_pickle.c` — pickle implementation
- `Modules/socketmodule.c` — socket module
- `Modules/_ssl.c` — SSL module
- `Modules/_weakref.c` — weakref module
- `Modules/clinic/` — argument clinic generated files (do not edit directly)

### Standard library (C-backed)
- `Lib/importlib/_bootstrap.py` — pure Python import machinery
- `Lib/importlib/_bootstrap_external.py` — filesystem import machinery
- `Lib/importlib/util.py` — importlib utilities

## Common bug patterns

### Null byte in string passed to C API
`PyUnicode_AsUTF8()` returns a `\0`-terminated C string from the Unicode
object's internal buffer. When the Python string contains an embedded null byte
(e.g. `"codecs\x00junk"`), the C string is silently truncated to `"codecs"`.
Fix pattern: use `PyUnicode_AsUTF8AndSize()` which also returns the length,
then check `strlen(s) != size` to detect embedded nulls and raise `ValueError`.

### Reference counting errors
- Over-decref: `Py_DECREF` on a borrowed reference → use-after-free
- Under-incref: returning a borrowed reference without incrementing → dangling
- Fix pattern: audit all `Py_DECREF`/`Py_XDECREF` calls, ensure ownership
  transfer matches Python's ownership convention

### GIL and thread safety
- `_Py_atomic_*` operations required for data shared across threads
- `Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS` must bracket blocking I/O
- Dict/list resize not safe without GIL

### Import system bugs
- `sys.modules` cache bypassed when module name contains null bytes
- Circular import issues often in `_bootstrap.py`
- Frozen module name collision: `find_frozen` uses C string comparison

### Type slot errors
- Missing `tp_traverse` or `tp_clear` → reference cycle not collected
- Wrong `tp_hash` / `tp_richcompare` pairing → dict/set corruption
- `tp_dealloc` must call `PyObject_GC_UnTrack` before freeing members

## Null byte fix pattern (canonical)
```c
// Wrong — truncates at null byte:
const char *name = PyUnicode_AsUTF8(module_name);
if (!name) return NULL;

// Correct — detects embedded nulls:
Py_ssize_t size;
const char *name = PyUnicode_AsUTF8AndSize(module_name, &size);
if (!name) return NULL;
if ((Py_ssize_t)strlen(name) != size) {
    PyErr_SetString(PyExc_ValueError, "embedded null character in module name");
    return NULL;
}
```

## Repo guard
Fires on repos containing `Python/` + `Objects/` + `Include/` directories.
(Handled automatically by the repo guard system — do not add guard here.)

## Key test directories
- `Lib/test/` — main test suite; `test_import.py`, `test_types.py` etc.
- `Lib/test/test_capi/` — C API tests
- `Modules/_testcapi/` — C test module
- `Modules/_testinternalcapi/` — internal C API tests

## Debug commands
```bash
# Build with debug assertions
./configure --with-pydebug && make -j$(nproc)

# Run specific test
./python -m pytest Lib/test/test_import.py -v

# Check for reference leaks
./python -m test -R 3:3 test_import

# Valgrind (slow but thorough)
valgrind --leak-check=full ./python -c "import codecs"
```
