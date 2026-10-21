"""Learning from past runs: per (op, tool) outcome history on this machine.

Every attempt is appended to $RTH_STATE/history.jsonl. If a tool's last LEARN_WINDOW attempts for an
op all failed for a *persistent* reason (permissions, unsupported flag, wrong binary, unparseable
output) under the same PATH, the executor skips it next time and says why. Any success, a PATH
change, or RTH_NO_LEARN=1 resets that. Transient failures (timeouts, network) never cause a skip.
"""
import json
import os
import threading
import time

from .env import state_dir

PERSISTENT = {"permission-denied", "unsupported-flag", "identity-conflict", "malformed-output"}
LEARN_WINDOW = 3
MAX_LINES = 5000
_lock = threading.Lock()


def _path():
    return state_dir() / "history.jsonl"


def record(op, tool, failure, path_hash, seconds=None):
    rec = {"ts": round(time.time()), "op": op, "tool": tool, "failure": failure, "path_hash": path_hash,
           **({"seconds": seconds} if seconds is not None else {})}
    with _lock, _path().open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _load():
    p = _path()
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    if len(lines) > MAX_LINES:  # keep the file bounded
        lines = lines[-MAX_LINES:]
        p.write_text("\n".join(lines) + "\n")
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def learned_skip(op, tool, path_hash):
    """Reason string if this tool should be skipped for this op, else None."""
    if os.environ.get("RTH_NO_LEARN") == "1":
        return None
    recent = [r for r in _load() if r["op"] == op and r["tool"] == tool and r.get("path_hash") == path_hash][-LEARN_WINDOW:]
    if len(recent) == LEARN_WINDOW and all(r["failure"] in PERSISTENT for r in recent):
        kinds = sorted({r["failure"] for r in recent})
        return f"learned: failed the last {LEARN_WINDOW} times here ({', '.join(kinds)}); RTH_NO_LEARN=1 to retry"
    return None


def stats():
    agg = {}
    for r in _load():
        a = agg.setdefault(f"{r['op']}:{r['tool']}", {"op": r["op"], "tool": r["tool"], "attempts": 0, "ok": 0, "failures": {}, "seconds": []})
        a["attempts"] += 1
        if r["failure"]:
            a["failures"][r["failure"]] = a["failures"].get(r["failure"], 0) + 1
        else:
            a["ok"] += 1
        if r.get("seconds") is not None:
            a["seconds"].append(r["seconds"])
    rows = []
    for a in agg.values():
        s = sorted(a.pop("seconds"))
        a["median_seconds"] = s[len(s) // 2] if s else None
        a["success_rate"] = round(a["ok"] / a["attempts"], 2)
        rows.append(a)
    return sorted(rows, key=lambda a: (a["op"], -a["success_rate"]))
