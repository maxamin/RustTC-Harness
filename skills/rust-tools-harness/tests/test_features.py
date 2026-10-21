"""Locks, sandbox, learning, resume, install hints, constraint extraction, jq subset, delete/rollback."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ["RTH_STATE"] = tempfile.mkdtemp(prefix="rth-feat-state-")

from rth import api, history, locks, planner, registry  # noqa: E402
from rth.builtins import jq_lite, JqUnsupported  # noqa: E402


def env():
    return api.get_env(refresh=True)


class Features(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rth-feat-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self, f, **opts):
        return api.execute(planner.single_op_plan("x", "replace-apply", {"pattern": "old", "replacement": "new", "files": [str(f)]}),
                           env(), allow={"writes"}, options=opts)["steps"][0]

    @unittest.skipUnless(locks.fcntl and (shutil.which("sd") or shutil.which("perl")), "needs flock and sd|perl")
    def test_locks_block_concurrent_writer(self):
        f = self.tmp / "a.txt"
        f.write_text("old\n")
        ready, done = threading.Event(), threading.Event()

        def holder():
            with locks.hold([str(self.tmp)], "other"):  # lock the parent directory
                ready.set()
                done.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        ready.wait()
        s = self.apply(f, lock_wait=1)
        done.set()
        t.join()
        self.assertEqual(s["status"], "blocked")
        self.assertIn("locked by another run", s["error"])
        self.assertEqual(f.read_text(), "old\n")
        self.assertEqual(self.apply(f)["status"], "verified")

    @unittest.skipUnless(shutil.which("prlimit"), "needs prlimit")
    def test_resource_limits_applied_to_user_code(self):
        r = api.run_op("parallel", env(), {"commands": ["true"]}, allow={"exec"})
        self.assertTrue(any(a.startswith("prlimit") for a in r["steps"][0]["sandbox"]))
        r = api.run_op("parallel", env(), {"commands": ["true"]}, allow={"exec"}, options={"no_limits": True})
        self.assertNotIn("sandbox", r["steps"][0])

    @unittest.skipUnless(shutil.which("bwrap"), "needs bubblewrap")
    def test_sandbox_isolation(self):
        probe = f"touch /etc/rth-probe 2>/dev/null && echo RW_ETC || echo RO_ETC; touch {self.tmp}/x 2>/dev/null && echo RW_TMPDIR_HOST || echo RO_HOST_TMP"
        r = api.run_op("parallel", env(), {"commands": [probe]}, allow={"exec"}, options={"sandbox": True})
        s = r["steps"][0]
        if s["status"] != "verified":
            self.skipTest(f"bwrap not usable here (user namespaces?): {s}")
        out = s["data"]["results"][0]["stdout_tail"].split()
        self.assertIn("RO_ETC", out)
        self.assertIn("RO_HOST_TMP", out)  # host paths outside cwd/run dir are read-only

    def test_learning_skips_persistently_failing_tool(self):
        ph = env().data["path_hash"]
        for _ in range(history.LEARN_WINDOW):
            history.record("find-files", "fd", "unsupported-flag", ph)
        self.assertIsNotNone(history.learned_skip("find-files", "fd", ph))
        self.assertIsNone(history.learned_skip("find-files", "fd", "other-path"))
        history.record("find-files", "fd", None, ph)  # one success resets
        self.assertIsNone(history.learned_skip("find-files", "fd", ph))
        for _ in range(history.LEARN_WINDOW):
            history.record("find-files", "rg", "timeout", ph)  # transient: never learned
        self.assertIsNone(history.learned_skip("find-files", "rg", ph))

    def test_resume_reuses_verified_steps_only_when_fresh(self):
        (self.tmp / "f.txt").write_text("needle\n")
        plan = planner.instantiate(planner.workflows()["repo-intel"], {"path": str(self.tmp)})
        first = api.execute(plan, env())
        again = api.execute(plan, env(), resume=first["run"])
        reused = [s["id"] for s in again["steps"] if s.get("reused_from")]
        self.assertTrue(reused)
        self.assertTrue(all(s["status"] in ("verified", "not-applicable") for s in again["steps"] if s.get("reused_from")))
        stale = dict(first, finished_at=time.time() - 7200)
        from rth import executor
        old = executor.execute(plan, env(), resume=stale)
        self.assertFalse(any(s.get("reused_from") for s in old["steps"]))
        self.assertIn("older than", old["resume_note"])

    def test_install_hints_only_for_available_managers(self):
        e = env()
        hints = registry.install_hints(registry.tools()["dust"], e, limit=10)
        pms = set(e.data["package_managers"])
        for h in hints:
            self.assertTrue(h["via"].split()[0] in pms or h["via"] in ("github release", "cargo (builds from source)", "cargo-binstall (prebuilt)"), h)

    def test_constraint_extraction(self):
        c = planner.extract_constraints
        self.assertEqual(c("files bigger than 500MB from last week"), {"min_size": "500M", "changed_within": "1w"})
        self.assertEqual(c("python files")["ext"], "py")
        self.assertEqual(c("top 5 processes by memory")["sort"], "mem")
        self.assertEqual(c("scan ports 22, 80,443")["ports"], "22,80,443")
        self.assertTrue(c("search case-insensitive")["ignore_case"])

    def test_jq_subset(self):
        d = {"items": [{"id": 1, "t": "a"}, {"id": 2, "t": "b"}]}
        self.assertEqual(jq_lite('.items[] | select(.t == "a") | .id', [d]), [1])
        self.assertEqual(jq_lite("[.items[].id]", [d]), [[1, 2]])
        self.assertEqual(jq_lite(".items | length", [d]), [2])
        with self.assertRaises(JqUnsupported):
            jq_lite(".items | sort_by(.id)", [d])

    def test_safe_delete_graveyard_roundtrip(self):
        v = self.tmp / "victim.txt"
        v.write_text("keep me")
        plan = planner.single_op_plan("d", "safe-delete", {"paths": [str(v)]})
        plan["steps"][0]["tool"] = "builtin"
        r = api.execute(plan, env(), allow={"writes"})
        s = r["steps"][0]
        if s.get("tool") != "builtin":
            self.skipTest(f"a system trash tool handled it: {s.get('tool')}")
        self.assertFalse(v.exists())
        rep = api.rollback(r["run"], apply=True)
        self.assertEqual(v.read_text(), "keep me", rep)

    def test_created_dir_changed_later_is_conflict(self):
        src = self.tmp / "srcdir"
        src.mkdir()
        (src / "a").write_text("1")
        r = api.run_op("copy", env(), {"src": str(src), "dest": str(self.tmp / "dst")}, allow={"writes"})
        (self.tmp / "dst" / "new-file").write_text("user work")
        rep = api.rollback(r["run"], apply=True)
        self.assertEqual(rep["conflicts"], 1)
        self.assertTrue((self.tmp / "dst" / "new-file").exists())


if __name__ == "__main__":
    unittest.main()
