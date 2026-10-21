"""Tests for rust-tools-harness. Run: python3 -m unittest discover -s tests -v (from the skill dir)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "lib"))
os.environ["RTH_STATE"] = tempfile.mkdtemp(prefix="rth-test-state-")

from rth import env as envmod  # noqa: E402
from rth import executor, matcher, planner, recover, registry, validate  # noqa: E402
from rth import ops as O  # noqa: E402


def fake_bin(d, name, script):
    p = Path(d) / name
    p.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    p.chmod(0o755)


class Registry(unittest.TestCase):
    def test_every_catalogue_entry_indexed(self):
        cat = json.loads((SKILL / "registry" / "catalogue.json").read_text(encoding="utf-8"))
        self.assertEqual({e["id"] for e in cat}, set(registry.tools()))
        self.assertGreaterEqual(len(cat), 170)

    def test_tool_capabilities_exist_in_taxonomy(self):
        caps = registry.capabilities()
        for t in registry.tools().values():
            self.assertTrue(t["capabilities"], t["id"])
            for c in t["capabilities"]:
                self.assertTrue(c in caps, f"{t['id']} -> {c} missing from capabilities.json")

    def test_unknown_stays_unknown(self):
        for t in registry.tools().values():
            src = t["verification"]["binary_source"]
            self.assertIn(src, ("crates.io", "Cargo.toml", "curated", "unknown"))
            if src == "unknown":
                self.assertIsNone(t["binary"], t["id"])

    def test_statuses(self):
        allowed = {"active", "experimental", "inactive", "archived", "unverified"}
        for t in registry.tools().values():
            self.assertIn(t["status"], allowed)
        self.assertEqual(registry.tools()["ytop"]["status"], "archived")

    def test_ops_and_workflows_are_consistent(self):
        caps = registry.capabilities()
        for o in O.OPS.values():
            self.assertTrue(o.capability in caps, o.name)
            for i in o.impls:
                if i.registry_id:
                    self.assertIn(i.registry_id, registry.tools(), f"{o.name}:{i.tool}")
        for w in planner.workflows().values():
            ids = [s["id"] for s in w["steps"]]
            self.assertEqual(len(ids), len(set(ids)), w["name"])
            for s in w["steps"]:
                self.assertIn(s["op"], O.OPS, f"{w['name']}:{s['id']}")
                for d in s.get("depends_on", []) + s.get("verified_by", []):
                    self.assertIn(d, ids)

    def test_readme_in_sync(self):
        """A tool added to the awesome-list README must get curated metadata (run build_registry parse/fetch/build)."""
        from rth import api
        h = api.registry_health()
        if "readme" not in h:
            self.skipTest("catalogue README not next to the skill")
        self.assertTrue(h["in_sync"], "README and registry differ: " + json.dumps(h["readme"]) +
                        ". Add curated.json entries, then: scripts/build_registry.py parse && fetch && build")

    def test_search(self):
        top = [t["id"] for _, t, _ in registry.search("disk usage", limit=6)]
        self.assertTrue({"dust", "dua-cli"} & set(top), top)

    def test_no_rm_fallback_for_safe_delete(self):
        tools = [i.tool for i in O.OPS["safe-delete"].impls]
        self.assertNotIn("rm", tools)


class Pieces(unittest.TestCase):
    def test_resolve_keeps_types(self):
        ctx = {"params": {"n": 3, "files": ["a", "b"]}, "steps": {"s": {"data": {"files": ["x"]}}}}
        self.assertEqual(executor.resolve("${params.files}", ctx), ["a", "b"])
        self.assertEqual(executor.resolve("${steps.s.data.files}", ctx), ["x"])
        self.assertEqual(executor.resolve("n=${params.n}", ctx), "n=3")
        self.assertIsNone(executor.resolve("${params.missing}", ctx))

    def test_classify(self):
        self.assertEqual(recover.classify(exit_code=2, stderr="error: unexpected argument '--x' found"), "unsupported-flag")
        self.assertEqual(recover.classify(exit_code=1, stderr="Permission denied"), "permission-denied")
        self.assertEqual(recover.classify(exit_code=0, parse_error="bad"), "malformed-output")
        self.assertEqual(recover.classify(timed_out=True), "timeout")
        self.assertIsNone(recover.classify(exit_code=1, ok_exit=(0, 1)))

    def test_exit_code_alone_is_not_evidence(self):
        v = validate.check({"exit_in": [0]}, exit_code=0, data=None, stdout="", inputs={}, parsed=None)
        self.assertTrue(v["passed"])
        self.assertFalse(v["evidence"])

    def test_waves_and_cycles(self):
        w = executor.waves([{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c"}])
        self.assertEqual([[s["id"] for s in x] for x in w], [["a", "c"], ["b"]])
        with self.assertRaises(ValueError):
            executor.waves([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}])

    def test_scope(self):
        self.assertTrue(executor._scope_ok("10.0.0.5", "10.0.0.0/24"))
        self.assertFalse(executor._scope_ok("10.0.1.5", "10.0.0.0/24"))
        self.assertFalse(executor._scope_ok("example.com", None))

    def test_small_model_affordances(self):
        env = envmod.detect()
        p = planner.single_op_plan("x", "search-content", {"identifier": "foo", "dir": ".", "bogus": 1})
        self.assertEqual(p["params"], {"pattern": "foo", "path": "."})
        self.assertTrue(any("bogus" in w for w in p["warnings"]))
        with self.assertRaises(ValueError):
            planner.plan("x", env, workflow="made_up_workflow")
        sel = matcher.select("find_identifier_usage", env)
        self.assertEqual(sel["selection"]["op"], "search-content")
        self.assertEqual(sel["next"]["tool"], "rth_run")
        self.assertIn("pattern", sel["next"]["arguments"]["params"])
        self.assertEqual(matcher.select("where is old_name used", env)["selection"]["op"], "search-content")

    def test_intent_routing_accuracy(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import intent_eval
        acc, misses = intent_eval.evaluate()
        self.assertGreaterEqual(acc, 0.95, f"routing regressed: {misses}")

    def test_matcher_prefers_passive(self):
        sel = matcher.select("show listening ports", envmod.detect(refresh=True))
        self.assertEqual(sel["selection"]["op"], "net-connections")


class Execution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rth-test-"))
        self.fake = self.tmp / "bin"
        self.fake.mkdir()
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.fake}:{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self):
        return envmod.detect(refresh=True)

    def run_op(self, name, params, **kw):
        return executor.execute(planner.single_op_plan(name, name, params), self.env(), **kw)

    def test_unsupported_flag_falls_back(self):
        fake_bin(self.fake, "fd", '[ "$1" = --version ] && echo "fd 1" && exit 0\necho "error: unexpected argument \'--glob\'" >&2; exit 2\n')
        (self.tmp / "data").mkdir()
        (self.tmp / "data" / "x.txt").write_text("hi", encoding="utf-8")
        r = self.run_op("find-files", {"path": str(self.tmp / "data"), "ext": "txt"})
        s = r["steps"][0]
        self.assertEqual(s["status"], "verified")
        self.assertNotEqual(s["tool"], "fd")
        self.assertEqual(s["attempts"][0]["failure"], "unsupported-flag")
        self.assertTrue(any(x["path"].endswith("x.txt") for x in s["data"]))

    def test_malformed_output_falls_back(self):
        fake_bin(self.fake, "rg", '[ "$1" = --version ] && echo "ripgrep 1" && exit 0\necho garbage\n')
        f = self.tmp / "f.txt"
        f.write_text("needle\n", encoding="utf-8")
        r = self.run_op("search-content", {"pattern": "needle", "path": str(self.tmp)})
        s = r["steps"][0]
        self.assertEqual(s["attempts"][0]["failure"], "malformed-output")
        self.assertEqual(s["data"]["match_count"], 1)

    def test_timeout_retries_then_falls_back(self):
        fake_bin(self.fake, "hexyl", '[ "$1" = --version ] && echo "hexyl 1" && exit 0\nsleep 20\n')
        f = self.tmp / "b.bin"
        f.write_bytes(b"\x00\x01binary")
        plan = planner.single_op_plan("hex", "hex-view", {"path": str(f), "length": 8})
        plan["steps"][0]["timeout"] = 1
        s = executor.execute(plan, self.env())["steps"][0]
        hexyl = [a for a in s["attempts"] if a["tool"] == "hexyl"]
        self.assertEqual(len(hexyl), 2)
        self.assertTrue(all(a["timed_out"] for a in hexyl))
        self.assertIn(s["status"], ("verified", "failed"))  # xxd/od may be absent on minimal systems

    def test_identity_conflict_is_detected(self):
        fake_bin(self.fake, "hx", '[ "$1" = --version ] && echo "helix 25.01" && exit 0\n')
        self.assertEqual(self.env().identity(registry.tools()["hex"])["state"], "conflict")

    def test_write_ops_need_approval(self):
        src = self.tmp / "a.txt"
        src.write_text("x", encoding="utf-8")
        r = self.run_op("copy", {"src": str(src), "dest": str(self.tmp / "b.txt")})
        self.assertEqual(r["status"], "needs-approval")
        self.assertFalse((self.tmp / "b.txt").exists())
        r = self.run_op("copy", {"src": str(src), "dest": str(self.tmp / "b.txt")}, allow={"writes"})
        self.assertEqual(r["status"], "verified")

    def test_port_scan_requires_scope(self):
        r = self.run_op("port-scan", {"target": "192.0.2.1", "authorized_scope": "198.51.100.0/24"}, allow={"network"})
        self.assertEqual(r["steps"][0]["status"], "blocked")

    def test_extract_refuses_non_empty_dest(self):
        dest = self.tmp / "out"
        dest.mkdir()
        (dest / "keep").write_text("x", encoding="utf-8")
        r = self.run_op("archive-extract", {"archive": str(self.tmp / "a.tar"), "dest": str(dest)}, allow={"writes"})
        self.assertEqual(r["steps"][0]["status"], "blocked")

    @unittest.skipUnless(shutil.which("git") and shutil.which("rg") and (shutil.which("sd") or shutil.which("perl")), "needs git, rg, sd|perl")
    def test_search_replace_workflow(self):
        repo = self.tmp / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "m.rs").write_text("fn old_name() {}\nfn main() { old_name(); }\n", encoding="utf-8")
        g = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*g, "add", "-A"], check=True)
        subprocess.run([*g, "commit", "-qm", "i"], check=True)
        w = planner.workflows()["search-replace"]
        plan = planner.instantiate(w, {"pattern": "old_name", "replacement": "new_name", "path": str(repo)})
        r = executor.execute(plan, self.env())
        self.assertEqual(r["status"], "needs-approval")
        self.assertIn("old_name", (repo / "src" / "m.rs").read_text(encoding="utf-8"))
        r = executor.execute(plan, self.env(), approve={"apply"})
        self.assertEqual(r["status"], "verified", json.dumps(r["remaining"]))
        self.assertNotIn("old_name", (repo / "src" / "m.rs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
