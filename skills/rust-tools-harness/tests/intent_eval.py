"""Intent routing accuracy on tests/fixtures/intents.json (planner: workflow or op)."""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-eval-"))
from rth import env as envmod, planner  # noqa: E402


def route(text, env):
    p = planner.plan(text, env)
    return p.get("workflow") or (p["steps"][0]["op"] if p.get("steps") else None)


def evaluate(verbose=False):
    env = envmod.detect()
    cases = json.loads((HERE / "fixtures" / "intents.json").read_text())
    misses = [(t, want, route(t, env)) for t, want in cases]
    misses = [m for m in misses if m[2] not in (m[1] if isinstance(m[1], list) else [m[1]])]
    if verbose:
        for t, want, got in misses:
            print(f"  MISS {t!r}: want {want}, got {got}")
    return 1 - len(misses) / len(cases), misses


if __name__ == "__main__":
    acc, _ = evaluate(verbose=True)
    print(f"accuracy {acc:.0%}")
