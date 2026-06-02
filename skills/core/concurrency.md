# Skill: Concurrency & Race Condition Bugs

## Trigger keywords
race condition, deadlock, livelock, thread, mutex, lock, semaphore,
atomic, memory order, happens-before, data race, concurrent, parallel,
pthread, std::thread, tokio, goroutine, channel, actor, async, await,
TOCTOU, ABA problem, spinlock, RWlock, condition variable, thread sanitizer,
ThreadSanitizer, TSan, helgrind, valgrind, memory barrier, fence

## Mental model: the three questions
For any concurrency bug ask:
1. **What shared state exists?** (globals, heap objects, files, sockets)
2. **Who reads/writes it?** (which threads, goroutines, tasks)
3. **What ordering is assumed?** (what must happen before what)

The bug is almost always a violation of assumption 3.

## Common bug patterns

### Data race
Two threads access the same memory, at least one writes, no synchronisation.
- **C/C++**: ThreadSanitizer (`-fsanitize=thread`) gives exact race report with both stack traces
- **Go**: `go test -race` / `go run -race`
- **Rust**: data races on `Send`/`Sync` types are compile-time errors; races on `UnsafeCell` still possible
- Fix: add a mutex around all accesses OR make the operation atomic

### TOCTOU (Time-of-check to time-of-use)
```
if (condition) {        ← check
    use_thing();        ← use   ← another thread changes condition here
}
```
Fix: hold the lock across both check and use, or use atomic compare-and-swap.

### Deadlock
Two threads each hold a lock the other needs.
- lockdep (kernel), TSan, or Helgrind will report this
- Fix: establish a global lock ordering and always acquire in that order
- Alternative: use `try_lock` with backoff, or replace two locks with one

### ABA problem
Thread reads value A, another thread changes A→B→A, first thread CAS succeeds
incorrectly thinking nothing changed.
- Affects lock-free data structures using CAS
- Fix: tagged pointers / version counters alongside the value

### Spurious wakeup on condition variables
`pthread_cond_wait` / `std::condition_variable::wait` can wake without notification.
Always re-check the predicate in a loop:
```cpp
while (!ready) cv.wait(lock);   // correct
if (!ready) cv.wait(lock);      // wrong
```

### Async / coroutine races
- Shared mutable state accessed across `await` points without holding a lock
- Cancellation: task cancelled between an allocation and its cleanup — use RAII

## Investigation workflow
1. Run with ThreadSanitizer / race detector first — it gives exact evidence
2. If non-deterministic: add logging with thread IDs and timestamps, run many times
3. Reduce to minimal reproducer — remove all unrelated threads
4. Draw a happens-before diagram for the failing execution

## Key tools
```bash
# C/C++ ThreadSanitizer
clang -fsanitize=thread -g -O1 prog.c && ./a.out

# Go race detector
go test -race ./...

# Valgrind Helgrind (slower but no recompile needed)
valgrind --tool=helgrind ./prog

# Linux kernel: lockdep
CONFIG_PROVE_LOCKING=y
CONFIG_DEBUG_LOCKDEP=y
```

## Heuristics from experienced engineers
- If a bug only reproduces under load or on multi-core machines → suspect race
- If adding a `printf` / log makes the bug disappear → Heisenbug, almost certainly a race (timing changed)
- If the bug is intermittent and affects shared data → draw the interleaving before writing any fix
- Never add a `sleep` as a fix for a race — it masks it, it will return
