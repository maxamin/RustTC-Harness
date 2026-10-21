"""Parser snapshot tests: real (or source-derived, see fixtures/SOURCES.md) tool output -> normalized records."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-test-state-"))

from rth import ops as O  # noqa: E402

FX = HERE / "fixtures"


def fx(name):
    return (FX / name).read_text(encoding="utf-8")


def impl(op, tool):
    return next(i for i in O.OPS[op].impls if i.tool == tool)


class Parsers(unittest.TestCase):
    def test_rg_json(self):
        d = impl("search-content", "rg").parse(fx("rg.jsonl"), {})
        self.assertEqual(d["match_count"], 3)
        self.assertEqual(d["files"], ["./notes.txt", "./src/main.rs"])
        self.assertEqual({"path", "line", "text"}, set(d["matches"][0]))
        self.assertEqual(d["stats"]["matched_lines"], 3)

    def test_rg_json_limit(self):
        d = impl("search-content", "rg").parse(fx("rg.jsonl"), {"limit": 1})
        self.assertEqual(len(d["matches"]), 1)
        self.assertEqual(d["match_count"], 3)
        self.assertTrue(d["truncated"])

    def test_rg_malformed_raises(self):
        with self.assertRaises(ValueError):
            impl("search-content", "rg").parse("not json\n", {})

    def test_git_grep(self):
        d = impl("search-content", "git").parse(fx("git_grep.txt"), {})
        self.assertEqual(d["match_count"], 3)
        self.assertEqual(d["matches"][1], {"path": "src/main.rs", "line": 1, "text": "fn old_name() {}"})

    def test_rg_replace_preview(self):
        d = impl("replace-preview", "rg").parse(fx("rg_replace.txt"), {})
        self.assertEqual(d["change_count"], 3)
        self.assertIn({"path": "./src/main.rs", "line": 1, "after": "fn new_name() {}"}, d["changes"])

    def test_fd(self):
        self.assertEqual(impl("find-files", "fd").parse(fx("fd.txt"), {}), [{"path": "./src/main.rs"}])

    def test_git_status_v2(self):
        d = impl("git-status", "git").parse(fx("git_status_v2.txt"), {})
        self.assertEqual(d["branch"], "master")
        self.assertEqual(d["changed"], [{"xy": ".M", "path": "src/main.rs"}])
        self.assertIn("untracked.txt", d["untracked"])
        self.assertFalse(d["clean"])

    def test_git_numstat(self):
        d = impl("git-diff-stat", "git").parse(fx("git_numstat.txt"), {})
        self.assertEqual(d["files"], [{"path": "src/main.rs", "added": 1, "deleted": 0}])

    def test_tokei(self):
        d = impl("code-stats", "tokei").parse(fx("tokei.json"), {})
        self.assertTrue(any(r["language"] == "Rust" for r in d["languages"]))
        self.assertIsInstance(d["total"]["code"], int)

    def test_du(self):
        rows = impl("disk-usage", "du").parse(fx("du.txt"), {})
        self.assertEqual(rows[0], {"path": ".", "bytes": 132 * 1024})
        self.assertEqual([r["bytes"] for r in rows], sorted((r["bytes"] for r in rows), reverse=True))

    def test_du_rejects_garbage(self):
        with self.assertRaises(ValueError):
            impl("disk-usage", "du").parse("12M\t.\n", {})

    def test_dust_bytes(self):
        rows = impl("disk-usage", "dust").parse(fx("dust_bytes.json"), {})
        self.assertEqual(rows[0], {"path": ".", "bytes": 4096, "depth": 0})
        self.assertIn({"path": "./src/a", "bytes": 1024, "depth": 2}, rows)

    def test_diskus(self):
        # piped (what the harness sees) and TTY formats
        self.assertEqual(impl("dir-size", "diskus").parse(fx("diskus.txt"), {"path": "x"}), {"path": "x", "bytes": 1036288})
        self.assertEqual(impl("dir-size", "diskus").parse(fx("diskus_tty.txt"), {"path": "x"}), {"path": "x", "bytes": 1036288})

    def test_df(self):
        rows = impl("disk-free", "df").parse(fx("df.txt"), {})
        self.assertEqual(rows[0]["use_pct"], 96)
        self.assertEqual(rows[0]["mount"], "/mnt/data volume")

    def test_ps(self):
        rows = impl("process-list", "ps").parse(fx("ps_linux.txt"), {})
        self.assertEqual(rows[0]["command"], "rustc")
        self.assertEqual(rows[2]["command"], "Web Content")
        self.assertEqual(impl("process-list", "ps").parse(fx("ps_linux.txt"), {"filter": "journal"})[0]["pid"], 1201)

    def test_hyperfine(self):
        tmp = Path(tempfile.mkdtemp()) / "hf.json"
        shutil.copy(FX / "hyperfine.json", tmp)
        d = impl("benchmark", "hyperfine").parse("", {"_export": str(tmp)})
        r = d["results"][0]
        self.assertEqual(r["command"], "true")
        self.assertEqual(r["runs"], 3)
        self.assertEqual(r["exit_codes"], [0])
        self.assertFalse(tmp.exists())  # export file is cleaned up

    def test_jaq(self):
        self.assertEqual(impl("json-query", "jaq").parse(fx("jaq.txt"), {}), [1, 2])


if __name__ == "__main__":
    unittest.main()
