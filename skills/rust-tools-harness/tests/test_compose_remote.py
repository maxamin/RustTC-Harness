"""Composition/foreach, remote targets (fake ssh transport), git writes + undo, gc/stats, read-only sandbox, new ops."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ["RTH_STATE"] = tempfile.mkdtemp(prefix="rth-cr-state-")

from rth import api, maintenance, planner, sandbox  # noqa: E402

FAKE_SSH = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; --) shift; break;; *) shift; break;; esac; done
[ "$1" = "--" ] && shift
exec sh -c "$*"
"""


def env():
    return api.get_env(refresh=True)


def git_repo(root):
    g = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root


class Compose(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rth-cr-"))
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.rs").write_text("fn a() {}\n", encoding="utf-8")
        (self.tmp / "src" / "b.rs").write_text("fn b() {}\n", encoding="utf-8")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_then_with_foreach(self):
        p = planner.plan("find rust files then show each of them", env())
        self.assertEqual(p["workflow"], "composed")
        self.assertEqual([s["op"] for s in p["steps"]], ["find-files", "view-file"])
        self.assertEqual(p["steps"][1]["foreach_param"], "path")
        r = api.execute(p, env())
        view = r["steps"][1]
        self.assertEqual(view["status"], "verified")
        self.assertEqual(view["foreach"]["ran"], 2)

    def test_list_wiring_and_gate(self):
        p = planner.plan("list rust files then move them to the trash", env())
        self.assertEqual(p["steps"][1]["inputs"]["paths"], "${steps.s1-find-files.paths}")
        r = api.execute(p, env())
        self.assertEqual(r["status"], "needs-approval")
        self.assertTrue((self.tmp / "src" / "a.rs").exists())

    def test_independent_clauses_and_no_false_split(self):
        p = planner.plan("check disk space and show running processes", env())
        self.assertEqual(p["workflow"], "composed")
        self.assertEqual(planner.plan("open the zip and list its files", env())["workflow"], "archive-inspect")


class Remote(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rth-remote-"))
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "ssh").write_text(FAKE_SSH, encoding="utf-8")
        (self.tmp / "bin" / "ssh").chmod(0o755)
        (self.tmp / "f.txt").write_text("needle\n", encoding="utf-8")
        self.old = os.environ["PATH"]
        os.environ["PATH"] = f"{self.tmp / 'bin'}:{self.old}"

    def tearDown(self):
        os.environ["PATH"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("grep") or shutil.which("rg"), "needs grep or rg")
    def test_read_only_op_runs_through_transport(self):
        r = api.run_op("search-content", env(), {"pattern": "needle", "path": str(self.tmp)}, options={"target": "ssh:fake"})
        s = r["steps"][0]
        self.assertEqual(s["status"], "verified")
        self.assertNotEqual(s["tool"], "builtin")
        self.assertEqual(s["attempts"][-1]["target"], "ssh:fake")

    def test_writes_refused_remotely(self):
        r = api.run_op("copy", env(), {"src": "a", "dest": "b"}, allow={"writes"}, options={"target": "ssh:fake"})
        self.assertEqual(r["steps"][0]["status"], "blocked")

    def test_bad_target(self):
        from rth import remote
        with self.assertRaises(ValueError):
            remote.parse("ftp:host")


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitWrites(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo(Path(tempfile.mkdtemp(prefix="rth-git-")))

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def head(self):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def test_commit_gate_and_soft_undo(self):
        before = self.head()
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        params = {"path": str(self.repo), "message": "change", "files": ["a.txt"]}
        self.assertEqual(api.run_op("git-commit", env(), params)["status"], "needs-approval")
        r = api.run_op("git-commit", env(), params, allow={"writes"})
        self.assertEqual(r["status"], "verified")
        self.assertNotEqual(self.head(), before)
        api.rollback(r["run"], apply=True)
        self.assertEqual(self.head(), before)
        st = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("M  a.txt", st)  # work kept, staged

    def test_branch_undo(self):
        r = api.run_op("git-branch", env(), {"path": str(self.repo), "name": "feat", "switch": True}, allow={"writes"})
        self.assertEqual(r["status"], "verified")
        api.rollback(r["run"], apply=True)
        branches = subprocess.run(["git", "-C", str(self.repo), "branch", "--format=%(refname:short)"], capture_output=True, text=True).stdout.split()
        self.assertNotIn("feat", branches)

    def test_history_rewrite_needs_its_own_gate(self):
        r = api.run_op("git-absorb", env(), {"path": str(self.repo), "and_rebase": True}, allow={"writes"})
        self.assertEqual(r["status"], "needs-approval")


class Maintenance(unittest.TestCase):
    def test_gc_keeps_graveyards_and_respects_keep(self):
        runs = Path(os.environ["RTH_STATE"]) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            d = runs / f"20000101-00000{i}-000000"
            d.mkdir(exist_ok=True)
            (d / "result.json").write_text(json.dumps({"run": d.name, "status": "verified", "steps": []}), encoding="utf-8")
            os.utime(d, (time.time() - 90 * 86400, time.time() - 90 * 86400))
        g = runs / "20000101-000000-000000" / "graveyard"
        g.mkdir()
        (g / "0000-x.txt").write_text("deleted user file", encoding="utf-8")
        rep = maintenance.gc(max_age_days=30, keep=100)
        self.assertEqual(rep["removed_runs"], 4)
        self.assertTrue(g.exists())
        st = maintenance.stats(days=365)
        self.assertIn("runs_by_status", st)


class SandboxModes(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bwrap"), "needs bubblewrap")
    def test_readonly_cwd(self):
        tmp = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            r = api.run_op("parallel", env(), {"commands": ["touch ./x && echo RW || echo RO"]}, allow={"exec"},
                           options={"sandbox_readonly": True})
            s = r["steps"][0]
            if s["status"] != "verified":
                self.skipTest(f"bwrap unusable: {s.get('error')}")
            self.assertEqual(s["data"]["results"][0]["stdout_tail"].strip(), "RO")
            self.assertFalse((tmp / "x").exists())
        finally:
            os.chdir(cwd)

    @unittest.skipIf(os.name == "nt", "posix")
    def test_ulimit_fallback_when_no_prlimit(self):
        real = shutil.which
        shutil.which = lambda n, *a, **k: None if n == "prlimit" else real(n, *a, **k)
        try:
            sandbox.configure(sandbox.DEFAULT_LIMITS)
            argv = sandbox.wrap(["sh", "-c", "ulimit -t"])
            out = subprocess.run(argv, capture_output=True, text=True).stdout.strip()
        finally:
            shutil.which = real
            sandbox.clear()
        self.assertEqual(out, str(sandbox.DEFAULT_LIMITS["cpu_seconds"]))


class NewOps(unittest.TestCase):
    def test_calculator_is_safe(self):
        self.assertEqual(api.run_op("calculate", env(), {"expr": "2 ** 10"}, tool="builtin")["steps"][0]["data"]["result"], 1024)
        bad = api.run_op("calculate", env(), {"expr": "__import__('os').system('true')"}, tool="builtin")["steps"][0]
        self.assertNotEqual(bad.get("tool"), "builtin")  # builtin refused; maybe fend/eva answered

    def test_math_words(self):
        self.assertEqual(planner.math_expression("what's 2 to the power of 10"), "2 ** 10")
        self.assertEqual(planner.math_expression("convert 5 GiB to MB"), "5 GiB to MB")

    def test_identify_and_color(self):
        s = api.run_op("identify", env(), {"text": "d41d8cd98f00b204e9800998ecf8427e"}, tool="builtin")["steps"][0]
        if s["tool"] == "builtin":
            self.assertEqual(s["data"]["matches"][0]["name"], "md5")
        c = api.run_op("color", env(), {"color": "#ff8800"}, tool="builtin")["steps"][0]
        if c["tool"] == "builtin":
            self.assertEqual(c["data"]["rgb"], "rgb(255, 136, 0)")


if __name__ == "__main__":
    unittest.main()
