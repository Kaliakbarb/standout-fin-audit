"""Linux trusted fresh-exec launcher; contains no private financial state.

Run only inside the prepared container. Failure to install any isolation step is
fatal. The final exec replaces this interpreter before policy code executes.
"""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import sys


def _check(result: int, operation: str) -> None:
    if result < 0:
        raise RuntimeError(f"sandbox_setup_failed:{operation}")


def _seccomp(lib: ctypes.CDLL) -> None:
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("sandbox_setup_failed:seccomp_init")
    blocked = (
        "socket", "socketpair", "connect", "accept", "accept4", "bind", "listen",
        "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "pidfd_open",
        "fork", "vfork", "clone", "clone3", "kill", "tkill", "tgkill", "pidfd_send_signal",
        "mount", "umount2", "pivot_root", "chroot", "unshare", "setns",
        "bpf", "perf_event_open", "keyctl", "add_key", "request_key",
        "open_by_handle_at", "name_to_handle_at", "io_uring_setup", "execveat",
        "memfd_create", "userfaultfd", "kexec_load", "reboot",
    )
    try:
        for name in blocked:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0:
                _check(lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0), name)
        _check(lib.seccomp_load(context), "seccomp_load")
    finally:
        lib.seccomp_release(context)


def main() -> None:
    if sys.platform != "linux" or os.geteuid() != 0 or len(sys.argv) != 2:
        raise RuntimeError("secure_runtime_unavailable")
    policy = sys.argv[1]
    if policy not in {"simple", "silent", "random", "mark_all", "probe"}:
        raise RuntimeError("invalid_policy")
    root = "/sandbox"
    if not os.path.isfile(root + "/agent_worker.py"):
        raise RuntimeError("secure_runtime_unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    os.closerange(3, 65536)
    os.chroot(root)
    os.chdir("/")
    os.setgroups([])
    os.setresgid(65534, 65534, 65534)
    os.setresuid(65534, 65534, 65534)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    _check(libc.prctl(38, 1, 0, 0, 0), "no_new_privileges")
    _seccomp(seccomp)
    environment = {"LANG": "C.UTF-8", "TZ": "UTC"}
    os.execve("/usr/local/bin/python3.11", ["python3.11", "-I", "-S", "/agent_worker.py", "--policy", policy], environment)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(78) from None
