# Skill: Linux Kernel Bugs

## Trigger keywords
kernel, linux, mm, memory management, slab, kmalloc, kfree, use-after-free,
null deref, oops, panic, BUG_ON, WARN_ON, RCU, spinlock, mutex, softirq,
hardirq, interrupt, scheduler, cgroup, namespaces, syscall, ioctl, vfs,
ext4, btrfs, xfs, netdev, skb, socket, tcp, udp, eBPF, kprobes, ftrace,
kasan, ubsan, lockdep, syzbot, syzkaller

## Key source locations
- `mm/`                  — memory management (slab, vmalloc, mmap, OOM)
- `kernel/`              — core kernel (sched, locking, RCU, timers)
- `fs/`                  — filesystems
- `net/`                 — networking stack
- `drivers/`             — device drivers
- `include/linux/`       — kernel headers
- `arch/`                — architecture-specific code

## Common bug patterns

### Use-after-free
Object freed while another code path still holds a reference.
- Check all `kfree` / `kmem_cache_free` call sites for missing `refcount_dec_and_test` guards
- KASAN output gives exact allocation and free stack traces — read them before anything else
- RCU objects: free must happen in `call_rcu` callback, not synchronously

### Locking bugs (lockdep)
lockdep output names the exact lock class and acquisition order.
- `ABBA` deadlock: two locks acquired in opposite order across two paths
- Lock held across sleep: spinlock → sleeping function (GFP_KERNEL alloc, mutex, etc.)
- Missing lock on shared data: look for all writers and readers of the field

### NULL dereference
- Oops gives exact faulting instruction and register dump
- Use `addr2line -e vmlinux <address>` to map to source line
- Common cause: unchecked return value from `of_find_*`, `devm_*`, `platform_get_*`

### Memory leaks
- kmemleak report gives allocation site
- Check all error paths: every `goto err` must free everything allocated above it

### Syzbot / syzkaller bugs
- Always check the syzbot dashboard for existing reports of the same crash signature
- The reproducer C program is authoritative — compile and run it first
- Check `Reported-by: syzbot` in recent commits for related fixes

## Useful debug tools
```bash
# Decode oops address to source line
scripts/decode_stacktrace.sh vmlinux < oops.txt

# Find which commit introduced a bug
git bisect start
git bisect bad <bad-commit>
git bisect good <good-commit>

# KASAN build config
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y

# lockdep
CONFIG_PROVE_LOCKING=y
CONFIG_DEBUG_LOCKDEP=y
```

## Key subsystem maintainers
- Memory management: `@akpm` (Andrew Morton)
- Networking: `@davem` (David Miller), `@kuba` (Jakub Kicinski)
- Filesystems: `@tytso` (Ted Ts'o — ext4)
- RCU: `@paulmckrcu` (Paul McKenney)
- Scheduler: `@peterz` (Peter Zijlstra)

## Key test infrastructure
- `tools/testing/selftests/` — kernel selftests
- `lib/test_*.c` — in-kernel unit tests
- kselftest, kunit frameworks
