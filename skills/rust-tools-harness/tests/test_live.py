"""Live adapter tests: every installed implementation of every op, pinned, against a sandbox.

A test fails if the pinned tool is installed but the harness ended up using another one (a silent
fallback means the adapter is broken for that tool's version). Missing tools are skipped unless
listed in RTH_LIVE_REQUIRE (comma-separated binaries; CI sets this). Tools that need privileges
are skipped when they fail with permission-denied. RTH_LIVE_REPORT=path writes a JSON matrix.

safe-delete moves files into the user's trash, so it runs only with RTH_LIVE_WRITES=1 (CI).
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-live-state-"))

from rth import env as envmod  # noqa: E402
from rth import executor, planner  # noqa: E402
from rth import ops as O  # noqa: E402

REQUIRE = {b for b in os.environ.get("RTH_LIVE_REQUIRE", "").split(",") if b}
REPORT = []
SKIP_OPS = {"help"}  # exercised implicitly by env probes
# ops whose success depends on host setup (collector daemons, perf permissions, a reachable server):
# a failure here with an environment-type class is reported, not failed
ENV_DEPENDENT = {"resource-history", "cpu-profile", "syscall-trace", "http-test", "net-bandwidth",
                 "task-run", "site-check", "jj-log", "mail-list", "cache-stats",  # need a project/config/account
                 "trace-route", "port-scan"}  # raw sockets / privileges / network differ per runner
ENV_FAILURES = {"not-found", "permission-denied", "failed", "network-error"}


def make_sandbox():
    root = Path(tempfile.mkdtemp(prefix="rth-live-"))
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.rs").write_text("fn old_name() {}\nfn main() { old_name(); }\n", encoding="utf-8")
    (repo / "src" / "lib.rs").write_text("pub fn helper() -> u32 { 42 }\n", encoding="utf-8")
    (repo / "notes.md").write_text("# notes\nold_name here\n", encoding="utf-8")
    (repo / "big.bin").write_bytes(os.urandom(64 * 1024))
    (repo / "data.csv").write_text("name,age\nann,31\nbob,45\n", encoding="utf-8")
    (repo / "data.json").write_text('{"items":[{"id":1},{"id":2}]}', encoding="utf-8")
    (repo / "justfile").write_text("test:\n\techo ok\n", encoding="utf-8")
    g = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)
    (repo / "src" / "main.rs").write_text("fn old_name() {}\nfn main() { old_name(); old_name(); }\n", encoding="utf-8")
    with tarfile.open(root / "a.tar.gz", "w:gz") as t:
        t.add(repo / "data.csv", arcname="data.csv")
    with zipfile.ZipFile(root / "a.zip", "w") as z:
        z.write(repo / "data.json", arcname="data.json")
    (root / "p.py").write_text("print(sum(range(1000)))\n", encoding="utf-8")
    (repo / "Makefile").write_text("hello:\n\t@echo hello\n", encoding="utf-8")
    (root / "t.hurl").write_text("GET http://127.0.0.1:9/\nHTTP 200\n", encoding="utf-8")
    (root / "a.txt").write_text("left\nsame\n", encoding="utf-8")
    (root / "b.txt").write_text("right\nsame\n", encoding="utf-8")
    return root, repo


def cases(root, repo, tool, n):
    """op -> (params, allow). n makes destinations unique per run."""
    r = str(repo)
    return {
        "find-files": ({"path": r, "ext": "rs"}, ()),
        "search-content": ({"pattern": "old_name", "path": r}, ()),
        "view-file": ({"path": f"{r}/src/main.rs", "end": 2, "highlight": tool == "bat"}, ()),
        "hex-view": ({"path": f"{r}/big.bin", "length": 32}, ()),
        "list-dir": ({"path": r}, ()),
        "tree": ({"path": r, "depth": 2}, ()),
        "disk-usage": ({"path": r, "depth": 1}, ()),
        "dir-size": ({"path": r}, ()),
        "disk-free": ({}, ()),
        "process-list": ({"limit": 5}, ()),
        "system-info": ({}, ()),
        "json-query": ({"file": f"{r}/data.json", "filter": ".items | length"}, ()),
        "csv-profile": ({"file": f"{r}/data.csv"}, ()),
        "code-stats": ({"path": r}, ()),
        "repo-summary": ({"path": r}, ()),
        "git-status": ({"path": r}, ()),
        "git-log": ({"path": r, "n": 5}, ()),
        "git-diff-stat": ({"path": r}, ()),
        "diff": ({"a": str(root / "a.txt"), "b": str(root / "b.txt")}, ()),
        "benchmark": ({"commands": ["true"], "runs": 3, "warmup": 0}, {"exec"}),
        "replace-preview": ({"pattern": "old_name", "replacement": "new_name", "path": r}, ()),
        "replace-apply": ({"pattern": "helper", "replacement": f"helper{n}", "files": [f"{r}/src/lib.rs"]}, {"writes"}),
        "archive-list": ({"archive": str(root / ("a.zip" if tool == "unzip" else "a.tar.gz"))}, ()),
        "archive-extract": ({"archive": str(root / ("a.zip" if tool == "unzip" else "a.tar.gz")), "dest": str(root / f"x{n}")}, {"writes"}),
        "copy": ({"src": f"{r}/data.csv", "dest": str(root / f"copy{n}.csv")}, {"writes"}),
        "safe-delete": ({"paths": [str(_victim(root, n))]}, {"writes"}),
        "net-connections": ({"listening": True}, ()),
        "trace-route": ({"target": "127.0.0.1", "cycles": 1, "max_hops": 2}, {"network"}),
        "port-scan": ({"target": "127.0.0.1", "ports": "1-64", "authorized_scope": "127.0.0.1"}, {"network"}),
        "task-list": ({"path": r}, ()),
        "parallel": ({"commands": ["true", "echo hi"]}, {"exec"}),
        "csv-query": ({"file": f"{r}/data.csv", "sql": "SELECT name FROM data"}, ()),
        "fs-query": ({"path": r, "fields": "path, size", "where": "size gt 0"}, ()),
        "http-test": ({"file": str(root / "t.hurl")}, {"network"}),
        "cpu-profile": ({"command": ["true"], "output": str(root / f"fg{n}.svg")}, {"exec"}),
        "python-profile": ({"command": f"python3 {root / 'p.py'}", "output": str(root / f"py{n}.json")}, {"exec"}),
        "syscall-trace": ({"command": ["true"]}, {"exec"}),
        "net-bandwidth": ({"seconds": 1}, ()),
        "resource-history": ({"begin": "-10m"}, ()),
        "regex-generate": ({"examples": ["a1", "b22"]}, ()),
        "calculate": ({"expr": "2 + 2"}, ()),
        "identify": ({"text": "d41d8cd98f00b204e9800998ecf8427e"}, ()),
        "color": ({"color": "#ff8800"}, ()),
        "task-run": ({"task": {"make": "hello", "just": "test", "npm": "test", "cargo": "test", "mise": "test",
                               "mask": "test"}.get(tool, "test"), "runner": tool, "path": r}, {"exec"}),
        "csv-frequency": ({"file": f"{r}/data.csv", "column": "name"}, ()),
        "csv-select": ({"file": f"{r}/data.csv", "columns": "name"}, ()),
        "site-check": ({"path": r}, ()),
        "cache-stats": ({}, ()),
        "jj-log": ({"path": r}, ()),
        "mail-list": ({}, {"network"}),
        "git-commit": ({"path": r, "message": f"live {n}", "files": ["notes.md"]}, {"writes"}),
        "git-branch": ({"path": r, "name": f"live-{n}-{tool}"}, {"writes"}),
        "git-absorb-preview": ({"path": r}, ()),
        "git-absorb": ({"path": r}, {"history"}),
    }


def _victim(root, n):
    p = root / f"victim{n}.txt"
    p.write_text("delete me", encoding="utf-8")
    return p


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.repo = make_sandbox()
        cls.env = envmod.detect(refresh=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)
        if os.environ.get("RTH_LIVE_REPORT"):
            Path(os.environ["RTH_LIVE_REPORT"]).write_text(json.dumps(REPORT, indent=1), encoding="utf-8")

    def check(self, op_name):
        o = O.OPS[op_name]
        for n, impl in enumerate(o.impls):
            if impl.tool == "builtin":
                continue
            with self.subTest(op=op_name, tool=impl.tool):
                row = {"op": op_name, "tool": impl.tool}
                if not executor.resolve_binary(impl, self.env):
                    row["result"] = "missing"
                    REPORT.append(row)
                    if impl.tool in REQUIRE:
                        self.fail(f"{impl.tool} required by RTH_LIVE_REQUIRE but not on PATH")
                    continue
                if op_name == "safe-delete" and os.environ.get("RTH_LIVE_WRITES") != "1":
                    row["result"] = "skipped (RTH_LIVE_WRITES!=1)"
                    REPORT.append(row)
                    continue
                params, allow = cases(self.root, self.repo, impl.tool, n)[op_name]
                plan = planner.single_op_plan(op_name, op_name, params)
                plan["steps"][0]["tool"] = impl.tool
                res = executor.execute(plan, self.env, allow=allow)
                s = res["steps"][0]
                mine = [a for a in s["attempts"] if a.get("tool") == impl.tool]
                row.update(result=s["status"], used=s.get("tool"), attempts=[{k: a.get(k) for k in ("tool", "failure", "skipped", "reason", "stderr_tail")} for a in s["attempts"]])
                REPORT.append(row)
                if mine and mine[0].get("skipped") == "cannot-express":
                    continue  # documented applicability rule (e.g. tar for .zip)
                env_fail = ENV_FAILURES | ({"malformed-output"} if op_name == "trace-route" else set())  # mtr without sockets prints no JSON
                if op_name in ENV_DEPENDENT and any(a.get("failure") in env_fail for a in mine):
                    row["result"] = "env-dependent failure: " + ",".join(a.get("failure") or "" for a in mine)
                    continue
                if any(a.get("failure") == "permission-denied" for a in mine):
                    continue  # privileged tool without privileges: correct behaviour is to fall back
                self.assertEqual(s.get("tool"), impl.tool, f"{op_name}: pinned {impl.tool} but used {s.get('tool')}: {json.dumps(row['attempts'])[:800]}")
                self.assertIn(s["status"], ("verified", "completed-unverified"), json.dumps(s, default=str)[:800])


for _name in O.OPS:
    if _name not in SKIP_OPS:
        setattr(Live, "test_" + _name.replace("-", "_"), lambda self, n=_name: self.check(n))


if __name__ == "__main__":
    unittest.main()
