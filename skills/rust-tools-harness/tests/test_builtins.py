"""Every pure-Python fallback, forced, with an empty PATH (a machine with none of the usual tools)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-test-state-"))

from rth import env as envmod  # noqa: E402
from rth import executor, planner  # noqa: E402
from rth import ops as O  # noqa: E402

import test_live  # noqa: E402  (sandbox + cases)

NEEDS_TOOLS = {"cpu-profile", "syscall-trace", "repo-summary", "net-bandwidth"}  # builtins that orchestrate external tools
NETWORK_OK = {"http-test"}


class Builtins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.repo = test_live.make_sandbox()
        cls.old = os.environ["PATH"]
        empty = cls.root / "empty-bin"
        empty.mkdir()
        os.environ["PATH"] = str(empty)
        cls.env = envmod.detect(refresh=True)

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls.old
        envmod.detect(refresh=True)

    def test_all_builtins(self):
        cases = test_live.cases(self.root, self.repo, "builtin", 99)
        cases["http-test"] = ({"url": "http://127.0.0.1:9/"}, {"network"})
        cases["python-profile"] = ({"command": f"{sys.executable} {self.root / 'p.py'}"}, {"exec"})
        cases["diff"] = ({"a": str(self.root / "a.txt"), "b": str(self.root / "b.txt")}, ())
        ran = []
        for name, o in O.OPS.items():
            for impl in o.impls:
                if impl.tool != "builtin" or impl.requires or name in NEEDS_TOOLS or name not in cases:
                    continue
                with self.subTest(op=name):
                    params, allow = cases[name]
                    plan = planner.single_op_plan(name, name, params)
                    plan["steps"][0]["tool"] = "builtin"
                    s = executor.execute(plan, self.env, allow=allow, options={"no_learn": True})["steps"][0]
                    ran.append(name)
                    if name in NETWORK_OK:  # nothing listens on :9 -> a clean failure, not a crash
                        self.assertIn(s["status"], ("failed", "verification-failed"))
                        continue
                    self.assertEqual(s.get("tool"), "builtin", f"{name}: {s.get('attempts')}")
                    self.assertIn(s["status"], ("verified", "completed-unverified"), f"{name}: {s}")
        self.assertGreaterEqual(len(ran), 18, ran)


if __name__ == "__main__":
    unittest.main()
