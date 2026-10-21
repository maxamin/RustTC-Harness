"""Offline agent tests: fake local Ollama / OpenAI-compatible servers that script Qwen-Coder-like replies."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
os.environ.setdefault("RTH_STATE", tempfile.mkdtemp(prefix="rth-test-state-"))

from rth import agent as A  # noqa: E402


class FakeLLM:
    """Serves /api/tags + /api/chat (Ollama) and /v1/models + /v1/chat/completions (OpenAI) from a script."""

    def __init__(self, script, models=("qwen2.5-coder:7b",)):
        self.script, self.models, self.requests = list(script), list(models), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send({"models": [{"name": m} for m in fake.models]})
                elif self.path == "/v1/models":
                    self._send({"data": [{"id": m} for m in fake.models]})
                else:
                    self.send_error(404)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(req)
                msg = fake.script.pop(0)
                if self.path == "/api/chat":
                    self._send({"model": req["model"], "message": {"role": "assistant", **msg}, "done": True})
                else:
                    self._send({"choices": [{"message": {"role": "assistant", **msg}}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def tool_results(req):
    return [m["content"] for m in req["messages"] if m["role"] in ("tool", "user") and m is not req["messages"][1]]


class OfflineAgent(unittest.TestCase):
    def setUp(self):
        self.mcp = A.MCPClient()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.mcp.close()

    def test_qwen_style_loop_on_ollama(self):
        script = [
            # 1: Qwen Coder writes the call as text instead of native tool_calls
            {"content": '<tool_call>\n{"name": "rth_run", "arguments": {"op": "find-files", "params": {"path": "%s"}}}\n</tool_call>' % self.tmp},
            # 2: native call to a gated tool -> must be denied (non-interactive, nothing pre-authorized)
            {"content": "", "tool_calls": [{"function": {"name": "rth_run_approved", "arguments": {
                "op": "copy", "params": {"src": str(self.tmp / "a.txt"), "dest": str(self.tmp / "b.txt")}, "allow": ["writes"]}}}]},
            {"content": "Found 1 file; copying was not authorized."},
        ]
        llm = FakeLLM(script)
        try:
            backend, note = A.autodetect(llm.url, None, "ollama")
            self.assertEqual(backend.model, "qwen2.5-coder:7b", note)
            ag = A.Agent(backend, self.mcp, A.Policy(interactive=False))
            out = ag.run_llm("list files and copy a.txt")
        finally:
            llm.close()
        self.assertEqual(out["answer"], "Found 1 file; copying was not authorized.")
        self.assertFalse((self.tmp / "b.txt").exists())
        self.assertEqual(len(out["runs"]), 1)                      # only the read-only run happened
        self.assertEqual(out["verification"][0]["overall"], "verified")
        last = llm.requests[-1]["messages"]
        self.assertTrue(any("DENIED" in (m.get("content") or "") for m in last))
        self.assertTrue(any('"status": "verified"' in (m.get("content") or "") for m in last))
        self.assertIn("tools", llm.requests[0])                    # native tool schemas were offered
        self.assertEqual(llm.requests[0]["model"], "qwen2.5-coder:7b")

    def test_preauthorized_write_on_openai_compatible(self):
        script = [
            {"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "rth_run_approved", "arguments": json.dumps({
                "op": "copy", "params": {"src": str(self.tmp / "a.txt"), "dest": str(self.tmp / "c.txt")}, "allow": ["writes"]})}}]},
            {"content": "Copied."},
        ]
        llm = FakeLLM(script, models=("qwen3-coder:30b",))
        try:
            backend, _ = A.autodetect(llm.url, None, "openai")
            ag = A.Agent(backend, self.mcp, A.Policy({"writes"}, interactive=False))
            out = ag.run_llm("copy a.txt to c.txt")
        finally:
            llm.close()
        self.assertTrue((self.tmp / "c.txt").exists())
        self.assertEqual(out["verification"][0]["overall"], "verified")
        tool_msgs = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_1")

    def test_json_mode_and_step_limit(self):
        llm = FakeLLM([{"content": '{"tool": "rth_env", "arguments": {}}'}] * 3)
        try:
            backend, _ = A.autodetect(llm.url, None, "ollama")
            ag = A.Agent(backend, self.mcp, A.Policy(interactive=False), max_steps=2, tool_mode="json")
            out = ag.run_llm("loop forever")
        finally:
            llm.close()
        self.assertTrue(out["stopped"])
        self.assertNotIn("tools", llm.requests[0])                 # json mode: tools described in the prompt
        self.assertIn("rth_env", llm.requests[0]["messages"][0]["content"])

    def test_missing_model_message(self):
        llm = FakeLLM([], models=("llama3:8b",))
        try:
            backend, note = A.autodetect(llm.url, None, "ollama")
        finally:
            llm.close()
        self.assertIsNone(backend)
        self.assertIn("ollama pull qwen2.5-coder:7b", note)

    def test_planner_mode_without_llm(self):
        ag = A.Agent(None, A.InProcessTools(), A.Policy(interactive=False))
        out = ag.run_planner("count lines of code", {"path": str(self.tmp)})
        self.assertEqual(out["verification"][0]["overall"], "verified")

    def test_default_uses_no_llm_and_no_mcp(self):
        """`rth agent <goal>` with no flags: no network connection attempt, no MCP subprocess."""
        import contextlib
        import io
        import socket
        from unittest import mock
        from rth import cli

        def no_connect(*a, **k):
            raise AssertionError(f"unexpected network connection {a[1:]}")

        def no_mcp(*a, **k):
            raise AssertionError("MCP server started without --mcp")

        out = io.StringIO()
        with mock.patch.object(socket.socket, "connect", no_connect), mock.patch.object(A, "MCPClient", no_mcp), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["agent", "count lines of code", "-p", f"path={self.tmp}", "--no-prompt"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("verified", out.getvalue())

    def test_llm_flags_require_llm(self):
        with self.assertRaises(SystemExit) as cm:
            A.main(["x", "--model", "qwen2.5-coder:7b"])
        self.assertIn("--llm", str(cm.exception.code))

    def test_offline_guard(self):
        self.assertTrue(A.is_local_url("http://127.0.0.1:11434"))
        self.assertTrue(A.is_local_url("http://localhost:8080"))
        self.assertTrue(A.is_local_url("http://192.168.1.20:11434"))
        self.assertFalse(A.is_local_url("http://8.8.8.8:80"))

    def test_privileged_never_from_loop(self):
        ok, why = A.Policy({"writes"}, interactive=True, ask=lambda q: True).check("rth_run_approved", {"op": "x", "allow": ["privileged"]})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
