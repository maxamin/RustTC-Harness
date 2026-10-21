"""Sandboxing for steps that run user code (benchmark, parallel, profilers, syscall-trace).

Two layers, configured per step (thread-local, so concurrent steps don't interfere):
  * resource limits (default ON for runs-user-code): CPU seconds, address space, max file size,
    applied with util-linux `prlimit` (thread-safe; no preexec_fn). Without prlimit: none, reported.
  * isolation (opt-in, --sandbox): bubblewrap with no network, read-only root (host /tmp readable),
    writable cwd + run dir, private TMPDIR, new PID namespace. Without bwrap the step is refused rather than run unisolated.
Every wrapped command is recorded so the result shows exactly what protection applied.
"""
import os
import shutil
import sys
import threading

DEFAULT_LIMITS = {"cpu_seconds": 900, "mem_mb": 4096, "fsize_mb": 2048}
_t = threading.local()


class SandboxUnavailable(RuntimeError):
    pass


def configure(limits=None, isolate=False, writable=(), readonly_cwd=False):
    _t.limits, _t.isolate, _t.writable, _t.applied = limits, isolate, list(writable), []
    _t.readonly_cwd = readonly_cwd


def clear():
    configure(None, False, ())


def active():
    return bool(getattr(_t, "limits", None) or getattr(_t, "isolate", False))


def wrap(argv):
    argv = [str(a) for a in argv]
    limits, isolate = getattr(_t, "limits", None), getattr(_t, "isolate", False)
    applied = getattr(_t, "applied", [])
    if isolate and sys.platform == "darwin":
        argv = _sandbox_exec(argv)
        if not any(a.startswith("sandbox-exec") for a in applied):
            applied.append("sandbox-exec(no network, writes only to cwd + run dir + TMPDIR) [macOS, untested in CI so far]")
    elif isolate:
        bw = shutil.which("bwrap")
        if not bw:
            raise SandboxUnavailable("isolation requested (--sandbox) but bubblewrap (bwrap) is not installed"
                                     + ("" if os.name != "nt" else "; no isolation backend exists for Windows"))
        ro = getattr(_t, "readonly_cwd", False)
        writable = [w for w in {*([] if ro else [os.getcwd()]), *getattr(_t, "writable", [])} if os.path.exists(w)]
        binds = [x for w in sorted(writable) for x in ("--bind", w, w)]
        # private writable temp inside the first extra writable dir (the run dir); host /tmp stays visible read-only
        tmp = os.path.join(sorted(getattr(_t, "writable", []) or [os.getcwd()])[0], "sandbox-tmp")
        os.makedirs(tmp, exist_ok=True)
        argv = [bw, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", *binds, "--setenv", "TMPDIR", tmp,
                "--unshare-net", "--unshare-pid", "--die-with-parent", "--chdir", os.getcwd(), "--", *argv]
        if not any(a.startswith("bwrap") for a in applied):
            applied.append("bwrap(no network, read-only root, writable: " + ("run dir only" if ro else "cwd + run dir")
                           + ", private TMPDIR, pid namespace)")
    if limits:
        pl = shutil.which("prlimit")
        if pl:
            argv = [pl, f"--cpu={limits['cpu_seconds']}", f"--as={limits['mem_mb'] * 2**20}",
                    f"--fsize={limits['fsize_mb'] * 2**20}", "--", *argv]
            tag = f"prlimit(cpu {limits['cpu_seconds']}s, mem {limits['mem_mb']}MB, fsize {limits['fsize_mb']}MB)"  # noqa
            if tag not in applied:
                applied.append(tag)
        elif os.name != "nt":
            # portable POSIX fallback (macOS/BSD): ulimit inside sh, then exec the real command
            cpu, kb, blocks = limits["cpu_seconds"], limits["mem_mb"] * 1024, limits["fsize_mb"] * 2048
            script = (f"ulimit -t {cpu} 2>/dev/null; ulimit -v {kb} 2>/dev/null; ulimit -f {blocks} 2>/dev/null; "
                      'exec "$@"')
            argv = ["/bin/sh", "-c", script, "rth-limits", *argv]
            tag = f"ulimit(cpu {cpu}s, mem {limits['mem_mb']}MB where supported, fsize {limits['fsize_mb']}MB)"
            if tag not in applied:
                applied.append(tag)
        elif "limits-unavailable" not in applied:
            applied.append("limits-unavailable (Windows: no prlimit/ulimit)")
    _t.applied = applied
    return argv


def _sandbox_exec(argv):
    """macOS Seatbelt profile: deny network and writes outside cwd / run dir / TMPDIR."""
    se = shutil.which("sandbox-exec")
    if not se:
        raise SandboxUnavailable("sandbox-exec not found")
    allow = [*([] if getattr(_t, "readonly_cwd", False) else [os.getcwd()]), *getattr(_t, "writable", []), os.environ.get("TMPDIR", "/tmp"), "/private/tmp", "/private/var/folders"]
    subpaths = " ".join(f'(subpath "{os.path.realpath(p)}")' for p in allow if p and os.path.exists(p))
    profile = ("(version 1)(allow default)(deny network*)(allow network* (local unix))"
               f'(deny file-write*)(allow file-write* {subpaths} (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))')
    return [se, "-p", profile, *argv]


def wrap_shell(cmd):
    return wrap(["/bin/sh", "-c", cmd]) if os.name != "nt" else wrap(["cmd", "/c", cmd])


def argv(a):
    """Wrap an argv list when this thread's step is sandboxed; unchanged otherwise."""
    return wrap(a) if active() else [str(x) for x in a]


def shell_argv(cmd):
    return wrap_shell(cmd) if active() else (["/bin/sh", "-c", cmd] if os.name != "nt" else ["cmd", "/c", cmd])


def applied():
    return list(getattr(_t, "applied", []))
