"""Path locks for write steps: two runs (or two agents) never modify the same paths concurrently.

One lock file per absolute path under $RTH_STATE/locks/, held with flock for the duration of the
step. A lock on a directory also covers everything below it, by locking each ancestor path in
shared mode and the target itself exclusively. The lock file records the holder's run id and pid.
"""
import contextlib
import hashlib
import json
import os
import time

from .env import state_dir

try:
    import fcntl
except ImportError:  # Windows: no flock; locks degrade to a no-op and say so
    fcntl = None


class Locked(RuntimeError):
    pass


def _lockfile(path):
    d = state_dir() / "locks"
    d.mkdir(exist_ok=True)
    return d / (hashlib.sha1(path.encode()).hexdigest()[:20] + ".lock")


def _ancestors(path):
    out, cur = [], os.path.dirname(path)
    while cur and cur != os.path.dirname(cur):
        out.append(cur)
        cur = os.path.dirname(cur)
    return out


@contextlib.contextmanager
def hold(paths, run_id, wait=30):
    """Exclusive locks on `paths`, shared locks on their ancestors. Raises Locked after `wait` seconds."""
    if fcntl is None or not paths:
        yield {"locked": [], "note": None if paths else "nothing to lock"} if fcntl else {"locked": [], "note": "file locking unavailable on this platform"}
        return
    targets = sorted({os.path.abspath(p) for p in paths})
    plan = {}
    for t in targets:
        plan[t] = fcntl.LOCK_EX
        for a in _ancestors(t):
            plan.setdefault(a, fcntl.LOCK_SH)
    for t in targets:  # a target that is an ancestor of another stays exclusive
        plan[t] = fcntl.LOCK_EX
    held = []
    try:
        for path in sorted(plan):  # global order prevents deadlocks between runs
            fh = open(_lockfile(path), "a+")
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fh, plan[path] | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        fh.seek(0)
                        owner = fh.read().strip() or "unknown"
                        fh.close()
                        raise Locked(f"{path} is locked by another run ({owner})")
                    time.sleep(0.2)
            if plan[path] == fcntl.LOCK_EX:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"run": run_id, "pid": os.getpid(), "path": path}))
                fh.flush()
            held.append(fh)
        yield {"locked": targets}
    finally:
        for fh in reversed(held):
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()
