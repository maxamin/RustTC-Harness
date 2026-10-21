"""Rollback and MCP server tests."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-test-state-"))

from rth import api, mcp  # noqa: E402


class Rollback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rth-undo-"))
        self.env = api.get_env(refresh=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply_replace(self, f):
        if not (shutil.which("sd") or shutil.which("perl")):
            self.skipTest("needs sd or perl")
        return api.run_op("replace-apply", self.env, {"pattern": "old", "replacement": "new", "files": [str(f)]}, allow={"writes"})

    def test_restore_modified(self):
        f = self.tmp / "a.txt"
        f.write_text("old value\n", encoding="utf-8")
        res = self.apply_replace(f)
        self.assertEqual(res["steps"][0]["status"], "verified")
        self.assertEqual(res["steps"][0]["effect"]["modified"], 1)
        self.assertEqual(f.read_text(encoding="utf-8"), "new value\n")
        dry = api.rollback(res["run"])
        self.assertFalse(dry["applied"])
        self.assertEqual(f.read_text(encoding="utf-8"), "new value\n")
        api.rollback(res["run"], apply=True)
        self.assertEqual(f.read_text(encoding="utf-8"), "old value\n")

    def test_conflict_is_all_or_nothing(self):
        a, b = self.tmp / "a.txt", self.tmp / "b.txt"
        a.write_text("old a\n", encoding="utf-8")
        b.write_text("old b\n", encoding="utf-8")
        if not (shutil.which("sd") or shutil.which("perl")):
            self.skipTest("needs sd or perl")
        res = api.run_op("replace-apply", self.env, {"pattern": "old", "replacement": "new", "files": [str(a), str(b)]}, allow={"writes"})
        a.write_text("edited later\n", encoding="utf-8")
        rep = api.rollback(res["run"], apply=True)
        self.assertEqual(rep["conflicts"], 1)
        self.assertFalse(rep["applied"])
        self.assertEqual(b.read_text(encoding="utf-8"), "new b\n")  # untouched: nothing applied
        api.rollback(res["run"], apply=True, force=True)
        self.assertEqual((a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")), ("old a\n", "old b\n"))

    def test_created_paths_are_moved_not_deleted(self):
        src = self.tmp / "s.txt"
        src.write_text("x", encoding="utf-8")
        res = api.run_op("copy", self.env, {"src": str(src), "dest": str(self.tmp / "d.txt")}, allow={"writes"})
        self.assertEqual(res["steps"][0]["status"], "verified")
        rep = api.rollback(res["run"], apply=True)
        self.assertFalse((self.tmp / "d.txt").exists())
        moved = [a["moved_to"] for a in rep["actions"] if a.get("moved_to")]
        self.assertTrue(moved and Path(moved[0]).exists())


class MCP(unittest.TestCase):
    def rpc(self, *msgs):
        out = io.StringIO()
        mcp.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
        return [json.loads(ln) for ln in out.getvalue().splitlines()]

    def test_handshake_and_list(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(r), 2)  # notification gets no reply
        self.assertEqual(r[0]["result"]["serverInfo"]["name"], "rust-tools-harness")
        names = {t["name"] for t in r[1]["result"]["tools"]}
        self.assertTrue({"rth_run", "rth_run_approved", "rth_rollback_apply"} <= names)
        for t in r[1]["result"]["tools"]:
            ro = t["annotations"]["readOnlyHint"]
            self.assertEqual(ro, not (t["name"].endswith("_approved") or t["name"].endswith("_apply")), t["name"])

    def test_read_only_tool_cannot_write(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a").write_text("x", encoding="utf-8")
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "rth_run", "arguments": {
            "op": "copy", "params": {"src": str(tmp / "a"), "dest": str(tmp / "b")}}}})
        data = json.loads(r[0]["result"]["content"][0]["text"])
        self.assertEqual(data["status"], "needs-approval")
        self.assertFalse((tmp / "b").exists())

    def test_errors(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "nope"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "rth_info", "arguments": {"tool": "zzz"}}})
        self.assertEqual(r[0]["error"]["code"], -32601)
        self.assertTrue(r[1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
