"""State housekeeping (gc) and cross-run observability (stats).

gc never deletes a run that still holds user data (graveyard/ contents from safe-delete, or undo backups of a
run newer than the backup retention) unless forced; it reports what it kept and why.
"""
import json
import shutil
import time
from pathlib import Path

from . import history
from .audit import runs_dir
from .env import state_dir

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_KEEP = 50
AUTO_GC_EVERY = 86400  # at most once a day, triggered by a run


def _size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())


def _holds_user_data(run):
    g = run / "graveyard"
    return g.is_dir() and any(x.name != "moves.json" for x in g.iterdir())


def gc(max_age_days=DEFAULT_MAX_AGE_DAYS, keep=DEFAULT_KEEP, dry_run=False, force=False):
    runs = sorted((p for p in runs_dir().iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - max_age_days * 86400
    removed, kept, freed = [], [], 0
    for i, r in enumerate(runs):
        old = r.stat().st_mtime < cutoff or i >= keep
        if not old:
            continue
        if _holds_user_data(r) and not force:
            kept.append({"run": r.name, "why": "graveyard holds deleted files; restore with `rth rollback` or gc --force"})
            continue
        size = _size(r)
        if not dry_run:
            shutil.rmtree(r, ignore_errors=True)
        removed.append(r.name)
        freed += size
    for d in ("graveyard",):  # loose graveyards from builtin deletes without a run dir
        g = state_dir() / d
        if g.is_dir() and not force:
            kept.append({"path": str(g), "why": "standalone graveyard; remove manually or gc --force"})
    if not dry_run:
        (state_dir() / "last-gc").write_text(str(time.time()))
    return {"removed_runs": len(removed), "freed_bytes": freed, "kept": kept, "dry_run": dry_run,
            "remaining_runs": len(runs) - (0 if dry_run else len(removed)), "policy": {"max_age_days": max_age_days, "keep": keep}}


def maybe_auto_gc():
    """Called at run start: cheap, at most daily, never forced."""
    stamp = state_dir() / "last-gc"
    try:
        last = float(stamp.read_text())
    except (OSError, ValueError):
        last = 0
    if time.time() - last > AUTO_GC_EVERY:
        try:
            return gc()
        except OSError:
            return None
    return None


def stats(days=30):
    cutoff = time.time() - days * 86400
    by_status, by_op, slow, failures = {}, {}, [], {}
    n = 0
    for r in runs_dir().iterdir():
        res = r / "result.json"
        if not res.exists() or r.stat().st_mtime < cutoff:
            continue
        try:
            d = json.loads(res.read_text())
        except ValueError:
            continue
        n += 1
        by_status[d.get("status")] = by_status.get(d.get("status"), 0) + 1
        for s in d.get("steps", []):
            o = by_op.setdefault(s.get("op"), {"op": s.get("op"), "runs": 0, "verified": 0, "failed": 0})
            o["runs"] += 1
            o["verified"] += s.get("status") == "verified"
            o["failed"] += s.get("status") in ("failed", "verification-failed")
            for a in s.get("attempts", []):
                if a.get("seconds") is not None:
                    slow.append({"run": d["run"], "step": s["id"], "tool": a.get("tool"), "seconds": a["seconds"]})
                k = a.get("failure")
                if k:
                    key = f"{s.get('op')}:{a.get('tool')}:{k}"
                    failures[key] = failures.get(key, 0) + 1
    slow.sort(key=lambda x: -x["seconds"])
    learned = []
    for h in history.stats():
        if h["success_rate"] < 1 and h["attempts"] >= 3:
            learned.append({k: h[k] for k in ("op", "tool", "attempts", "success_rate", "failures")})
    disk = _size(state_dir()) if state_dir().exists() else 0
    return {"window_days": days, "runs": n, "runs_by_status": by_status,
            "ops": sorted(by_op.values(), key=lambda o: -o["runs"])[:20],
            "top_failures": sorted(({"what": k, "count": v} for k, v in failures.items()), key=lambda x: -x["count"])[:10],
            "slowest_attempts": slow[:10], "unreliable_tools": learned[:10], "state_bytes": disk}
