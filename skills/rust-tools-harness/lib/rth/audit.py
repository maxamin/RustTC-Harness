"""Append-only JSONL audit log per run: actions, argv, exit codes, artifacts, errors, verification."""
import json
import threading
import time
from pathlib import Path

from .env import state_dir


def runs_dir():
    d = state_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_run_id():
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time_ns() % 1_000_000):06d}"


class AuditLog:
    def __init__(self, run_id):
        self.run_id = run_id
        self.dir = runs_dir() / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"
        self._lock = threading.Lock()

    def event(self, kind, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run": self.run_id, "event": kind, **fields}
        with self._lock, self.path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def artifact(self, name, text):
        p = self.dir / name
        p.write_text(text or "")
        return str(p)


def list_runs(limit=10):
    runs = sorted((p for p in runs_dir().iterdir() if p.is_dir()), reverse=True)[:limit]
    out = []
    for r in runs:
        res = r / "result.json"
        summary = {}
        if res.exists():
            try:
                d = json.loads(res.read_text())
                summary = {"objective": d.get("objective"), "status": d.get("status")}
            except ValueError:
                pass
        out.append({"run": r.name, **summary})
    return out


def read(run_id):
    p = runs_dir() / run_id / "audit.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []
