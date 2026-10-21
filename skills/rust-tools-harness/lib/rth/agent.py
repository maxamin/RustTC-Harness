"""Offline agent over the harness. No LLM and no MCP unless explicitly requested.

    goal ─▶ Agent ─▶ planner (default: deterministic, no model)
                  └▶ --llm ollama|openai  (local model, default Qwen Coder)
             │ tool calls
             ▼
       InProcessTools (default: direct calls)  |  --mcp: MCPClient ──stdio──▶ bin/rth-mcp (or another server)

Guarantees enforced by this loop, not by the model:
  * offline: LLM endpoints must be loopback/private addresses unless allow_remote is set;
  * approval: *_approved / rth_rollback_apply calls need risk classes pre-authorized with --allow,
    or an interactive y/N from the user; otherwise the call is denied and the model is told so;
  * evidence: every run the agent executed is re-checked with rth_verify and appended to the final
    answer, so the report never rests on the model's own claim of success.
"""
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .env import state_dir

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
MCP_CMD = [sys.executable, str(SKILL_DIR / "bin" / "rth-mcp")]
# Small local models on CPU: fewer, smaller tool schemas keep the prompt short (prompt processing dominates).
CORE_TOOLS = ["rth_select", "rth_run", "rth_plan", "rth_execute", "rth_verify", "rth_run_approved", "rth_execute_approved"]
GATED_TOOLS = {"rth_run_approved", "rth_execute_approved", "rth_rollback_apply"}
OBS_LIMIT = 6000
# Default local model: Qwen2.5-Coder (tool-calling capable in Ollama). Any installed qwen*coder* model is preferred.
DEFAULT_MODEL = os.environ.get("RTH_AGENT_MODEL", "qwen2.5-coder:7b")
PREFERRED = re.compile(r"qwen[\d.]*-?coder", re.I)

SYSTEM_PROMPT = """You are an offline automation agent. You act ONLY through the provided tools (a harness over
Rust and Unix CLI tools). Method: select or plan -> run/execute -> inspect -> verify -> recover or replan.
Rules:
- Usually: rth_select once, then make the call in its "next" field with real values (e.g. op search-content,
  params {"pattern": "...", "path": "."}). Use rth_plan + rth_execute for multi-step goals. Pass explicit params.
- Read-only tools never change anything. A result with status "needs-approval" means the user must authorize:
  only then use rth_run_approved / rth_execute_approved with the needed "allow" classes. Never escalate privileges.
- A zero exit code is not success. Check step status ("verified") and the evidence; use rth_verify on run ids.
- On failure read recovery.advice, try rth_recover, or replan. Stop when the goal is met or progress is impossible.
- Finish with a short answer: what you found or changed, the evidence (counts, paths, numbers), caveats, run ids."""

JSON_MODE_HINT = """Tool calling format: reply with ONE JSON object and nothing else, either
{"tool": "<tool name>", "arguments": {...}}  or  {"final": "<answer for the user>"}.
Available tools:
"""


# ----------------------------------------------------------------------------- tool providers

class InProcessTools:
    """Default provider: the same tools the MCP server exposes, called directly (no subprocess, no protocol)."""

    def __init__(self):
        from . import mcp as server
        self._server = server
        self.server = {"name": "rust-tools-harness (in-process)"}
        self.instructions = server.INSTRUCTIONS
        self.tools = server.tool_list()

    def call(self, name, arguments):
        return self._server.call_text(name, arguments)

    def close(self):
        pass


class MCPClient:
    """Minimal stdio MCP client (newline-delimited JSON-RPC)."""

    def __init__(self, cmd=None, env=None):
        self.proc = subprocess.Popen(cmd or MCP_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, **(env or {})})
        self._id = 0
        info = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "rth-agent", "version": "1.0"}})
        self.server = info.get("serverInfo", {})
        self.instructions = info.get("instructions")
        self.notify("notifications/initialized")
        self.tools = self.request("tools/list")["tools"]

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server exited: " + self.proc.stderr.read()[-1000:])
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"MCP error {msg['error'].get('code')}: {msg['error'].get('message')}")
                return msg["result"]

    def call(self, name, arguments):
        r = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        return bool(r.get("isError")), text

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()
        for f in (self.proc.stdout, self.proc.stderr):
            f.close()


# ----------------------------------------------------------------------------- offline check

def is_local_url(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "") or host.endswith(".local"):
        return True
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(a.split("%")[0]).is_private or ipaddress.ip_address(a.split("%")[0]).is_loopback for a in addrs)


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ----------------------------------------------------------------------------- LLM backends

def _fn_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("inputSchema", {"type": "object"})}} for t in tools]


class OllamaBackend:
    name = "ollama"

    def __init__(self, url, model, timeout=900, num_ctx=8192):
        self.url, self.model, self.timeout, self.num_ctx = url.rstrip("/"), model, timeout, num_ctx

    @staticmethod
    def probe(url):
        tags = _get(url.rstrip("/") + "/api/tags")
        return [m["name"] for m in tags.get("models", [])]

    def chat(self, messages, tools):
        raw = _post(self.url + "/api/chat", {"model": self.model, "messages": messages, "stream": False,
                                             **({"tools": _fn_tools(tools)} if tools else {}), "keep_alive": "30m",
                                             "options": {"temperature": 0, "num_ctx": self.num_ctx}}, self.timeout)
        msg = raw.get("message", {})
        calls = [{"id": f"c{i}", "name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        return {"content": msg.get("content") or "", "tool_calls": calls, "assistant": {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}}

    def tool_message(self, call, text):
        return {"role": "tool", "content": text, "tool_name": call["name"]}


class OpenAIBackend:
    """Any OpenAI-compatible local server: llama.cpp llama-server, LM Studio, vLLM, LocalAI, Ollama /v1."""
    name = "openai"

    def __init__(self, url, model, timeout=900, api_key=None):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.api_key = api_key

    @staticmethod
    def probe(url):
        return [m["id"] for m in _get(url.rstrip("/") + "/v1/models").get("data", [])]

    def chat(self, messages, tools):
        payload = {"model": self.model, "messages": messages, "temperature": 0, **({"tools": _fn_tools(tools), "tool_choice": "auto"} if tools else {})}
        raw = _post(self.url + "/v1/chat/completions", payload, self.timeout)
        msg = raw["choices"][0]["message"]
        calls = [{"id": c.get("id") or f"c{i}", "name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            assistant["tool_calls"] = msg["tool_calls"]
        return {"content": msg.get("content") or "", "tool_calls": calls, "assistant": assistant}

    def tool_message(self, call, text):
        return {"role": "tool", "tool_call_id": call["id"], "content": text}


def _args(a):
    if isinstance(a, dict):
        return a
    if not a:
        return {}
    try:
        return json.loads(a)
    except ValueError:
        return {"_unparsed": a}


def extract_json_call(text):
    """JSON-in-text tool calls: our {"tool"|"final"} protocol, and Qwen/Hermes-style
    <tool_call>{"name": ..., "arguments": {...}}</tool_call> that Qwen Coder emits in plain content."""
    tagged = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text or "", re.S)
    if tagged:
        try:
            obj = json.loads(tagged[1])
            if isinstance(obj, dict) and "name" in obj:
                return {"tool": obj["name"], "arguments": _args(obj.get("arguments"))}
        except ValueError:
            pass
    for m in re.finditer(r"\{", text or ""):
        depth = 0
        for j in range(m.start(), len(text)):
            depth += text[j] == "{"
            depth -= text[j] == "}"
            if depth == 0:
                try:
                    obj = json.loads(text[m.start(): j + 1])
                except ValueError:
                    break
                if isinstance(obj, dict) and ("tool" in obj or "final" in obj):
                    return obj
                if isinstance(obj, dict) and isinstance(obj.get("name"), str) and "arguments" in obj:
                    return {"tool": obj["name"], "arguments": _args(obj["arguments"])}
                break
    return None


def pick_model(available, requested=None):
    """Explicit model if available; else an installed Qwen Coder; else the default name if present."""
    if requested:
        return next((a for a in available if a == requested or a.split(":")[0] == requested), None)
    coders = sorted((a for a in available if PREFERRED.search(a)), key=lambda a: (not a.startswith("qwen2.5-coder:7b"), a))
    if coders:
        return coders[0]
    return DEFAULT_MODEL if DEFAULT_MODEL in available else None


def autodetect(url=None, model=None, backend="auto"):
    """Find a local model server. Returns (backend_instance | None, note)."""
    cands = []
    if backend in ("auto", "ollama"):
        cands += [("ollama", url or "http://127.0.0.1:11434")]
    if backend in ("auto", "openai"):
        cands += [("openai", u) for u in ([url] if url else ["http://127.0.0.1:8080", "http://127.0.0.1:1234", "http://127.0.0.1:8000"])]
    for kind, u in cands:
        try:
            models = (OllamaBackend if kind == "ollama" else OpenAIBackend).probe(u)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            continue
        m = pick_model(models, model)
        if not m:
            if kind == "ollama":
                return None, f"Ollama is running at {u} but {model or DEFAULT_MODEL} is not pulled: run `ollama pull {model or DEFAULT_MODEL}`"
            continue
        return (OllamaBackend(u, m) if kind == "ollama" else OpenAIBackend(u, m)), f"{kind} at {u}, model {m}"
    return None, "no local model server found (tried " + ", ".join(u for _, u in cands) + ")"


# ----------------------------------------------------------------------------- policy

class Policy:
    """Gate for state-changing tools. The model can request; only the user can authorize."""

    def __init__(self, allow=(), interactive=None, ask=None):
        self.allow = set(allow)
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.ask = ask or self._ask_tty

    @staticmethod
    def _ask_tty(prompt):
        try:
            with open("/dev/tty", "r+") as tty:
                tty.write(prompt + " [y/N] ")
                tty.flush()
                return tty.readline().strip().lower() in ("y", "yes")
        except OSError:
            return False

    def check(self, name, args):
        if name not in GATED_TOOLS:
            return True, None
        want = set(args.get("allow", []))
        if "privileged" in want and "privileged" not in self.allow:
            return False, "privileged actions are never authorized from inside the agent loop"
        if name == "rth_rollback_apply":
            want = {"writes"}
        if want and want <= self.allow and not args.get("approve"):
            return True, f"pre-authorized: {sorted(want)}"
        if self.interactive:
            desc = json.dumps({k: v for k, v in args.items() if k != "plan"} | ({"plan_steps": [s.get("id") for s in args["plan"].get("steps", [])]} if "plan" in args else {}))[:400]
            if self.ask(f"Agent requests {name} {desc}"):
                return True, "approved interactively"
            return False, "user declined"
        return False, f"not authorized: needs {sorted(want) or 'approval'}; rerun the agent with --allow {','.join(sorted(want)) or '<class>'}"


# ----------------------------------------------------------------------------- the agent

class Agent:
    def __init__(self, backend=None, mcp=None, policy=None, max_steps=15, tool_mode="auto", log=None, tools_filter=None):
        self.backend = backend
        self.mcp = mcp or InProcessTools()  # pass MCPClient() to go through a real MCP server
        self.policy = policy or Policy()
        self.max_steps = max_steps
        self.tool_mode = tool_mode  # auto | native | json
        self.runs = []
        self.log_path = log or (state_dir() / "agent" / (time.strftime("%Y%m%d-%H%M%S") + ".jsonl"))
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.tools = [t for t in self.mcp.tools if not tools_filter or t["name"] in tools_filter]

    def _log(self, kind, **kw):
        with open(self.log_path, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": kind, **kw}, default=str) + "\n")

    def call_tool(self, name, args):
        ok, why = self.policy.check(name, args)
        self._log("tool_request", name=name, arguments=args, authorized=ok, why=why)
        if not ok:
            return True, f"DENIED: {why}"
        if name not in {t["name"] for t in self.tools}:
            return True, f"unknown tool {name}"
        is_err, text = self.mcp.call(name, args)
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("run") and data["run"] not in self.runs:
                self.runs.append(data["run"])
        except ValueError:
            pass
        self._log("tool_result", name=name, error=is_err, chars=len(text))
        return is_err, text

    def evidence(self):
        out = []
        for r in self.runs:
            _, text = self.mcp.call("rth_verify", {"run": r})
            try:
                v = json.loads(text)
                out.append({"run": r, "overall": v["overall"], "unverified_steps": v["unverified_steps"],
                            "evidence": {s["step"]: [c.get("detail") or c["check"] for c in s["evidence"]] for s in v["steps"]}})
            except (ValueError, KeyError):
                out.append({"run": r, "error": text[:200]})
        return out

    # --- LLM loop
    def run_llm(self, goal):
        native = self.tool_mode in ("auto", "native")
        sys_prompt = SYSTEM_PROMPT + ("\n" + self.mcp.instructions if self.mcp.instructions else "")
        if self.tool_mode == "json":
            sys_prompt += "\n" + JSON_MODE_HINT + "\n".join(f"- {t['name']}: {t['description']} params={json.dumps(t['inputSchema'].get('properties', {}))}" for t in self.tools)
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": goal}]
        self._log("start", goal=goal, backend=self.backend.name, model=self.backend.model, mode=self.tool_mode)
        seen = set()
        for step in range(1, self.max_steps + 1):
            t0 = time.monotonic()
            try:
                resp = self.backend.chat(messages, self.tools if native else None)
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                return self._finish(f"Stopped: the local model did not answer ({type(e).__name__}: {e}) after "
                                    f"{time.monotonic() - t0:.0f}s at step {step}. On a slow or busy CPU try --timeout 1800, "
                                    "--tools core, or a smaller model (qwen2.5-coder:3b).", step, stopped=True)
            self._log("model_reply", step=step, seconds=round(time.monotonic() - t0, 1),
                      tool_calls=[c["name"] for c in resp["tool_calls"]], content=(resp["content"] or "")[:500])
            calls = resp["tool_calls"]
            if not calls:  # JSON-in-text fallback, also for models that ignore native tools
                obj = extract_json_call(resp["content"])
                if obj and "tool" in obj:
                    calls = [{"id": f"j{step}", "name": obj["tool"], "arguments": obj.get("arguments") or {}, "_json": True}]
                elif obj and "final" in obj:
                    return self._finish(obj["final"], step)
            messages.append(resp["assistant"])
            if not calls:
                return self._finish(resp["content"], step)
            for c in calls:
                sig = json.dumps([c["name"], c["arguments"]], sort_keys=True)
                repeated = sig in seen
                seen.add(sig)
                is_err, text = self.call_tool(c["name"], c["arguments"])
                if repeated:
                    text += ("\n\nNOTE: you already made this exact call; the result is the same. Do not repeat it. "
                             "Follow the 'next' suggestion, call rth_run with explicit params, or give the final answer.")
                obs = text if len(text) <= OBS_LIMIT else text[:OBS_LIMIT] + f"... [truncated {len(text) - OBS_LIMIT} chars]"
                if c.get("_json"):
                    messages.append({"role": "user", "content": f"Result of {c['name']} ({'error' if is_err else 'ok'}):\n{obs}"})
                else:
                    messages.append(self.backend.tool_message(c, obs))
        return self._finish("Stopped: step limit reached before the goal was completed.", self.max_steps, stopped=True)

    # --- no-LLM autonomous loop
    def run_planner(self, goal, params=None):
        self._log("start", goal=goal, backend="planner")
        notes = []
        _, text = self.call_tool("rth_plan", {"objective": goal, **({"params": params} if params else {})})
        plan = json.loads(text)
        if plan.get("status") == "no-plan":
            return self._finish("No workflow or op matches this goal. Candidates:\n" + json.dumps(plan.get("discover"), indent=1)[:3000], 1)
        if plan.get("status") == "needs-input":
            return self._finish(f"The plan needs input: {plan.get('missing_params')}. Rerun with -p key=value.", 1)
        notes.append(f"plan: {plan.get('workflow') or [s['op'] for s in plan['steps']]}")
        _, text = self.call_tool("rth_execute", {"plan": plan, "brief": True})
        res = json.loads(text)
        if res["status"] == "needs-approval":
            want = sorted({c for c in (_risk_class(st) for st in plan["steps"]) if c})
            notes.append(f"read-only part done (run {res['run']}); gated steps need {want}")
            _, text = self.call_tool("rth_execute_approved", {"plan": plan, "allow": want, "brief": True})
            if text.startswith("DENIED"):
                return self._finish("\n".join(notes + [text]), 2, denied=True)
            res = json.loads(text)
        lines = notes + [f"run {res['run']}: {res['status']}"]
        for s in res["steps"]:
            lines.append(f"  {s['id']}: {s['status']}" + (f" via {s['tool']}" if s.get("tool") else "") +
                         (f" ({s['records']} records)" if "records" in s else "") + (f" - {s['error']}" if s.get("error") else ""))
        for r in res.get("remaining", []):
            lines.append(f"  remaining {r['step']} ({r['status']}): {r['why']}")
        return self._finish("\n".join(lines), 3)

    def _finish(self, answer, steps, stopped=False, denied=False):
        ev = self.evidence()
        self._log("finish", steps=steps, stopped=stopped, runs=self.runs)
        last = ev[-1].get("overall") if ev else None
        status = "denied" if denied else "stopped" if stopped else (last or "answered")
        return {"answer": answer, "status": status, "steps": steps, "stopped": stopped, "runs": self.runs,
                "verification": ev, "transcript": str(self.log_path)}


def _risk_class(step):
    from . import ops as O
    o = O.OPS.get(step.get("op"))
    return {O.WRITES: "writes", O.NETWORK: "network", O.RUNS_CODE: "exec"}.get(o.risk) if o else None


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="rth-agent", description="Offline agent over the rust-tools-harness. "
                                 "Default: deterministic planner, in-process tools (no LLM, no MCP).")
    ap.add_argument("goal", nargs="+")
    ap.add_argument("--llm", choices=["ollama", "openai"],
                    help="use a local model; ollama = :11434, openai = OpenAI-compatible server (llama.cpp, LM Studio, vLLM)")
    ap.add_argument("--url", help="LLM server base URL (must be local unless --allow-remote-llm)")
    ap.add_argument("--model", help=f"with --llm: default an installed qwen*coder* model, else {DEFAULT_MODEL} (env RTH_AGENT_MODEL)")
    ap.add_argument("--tool-mode", choices=["auto", "native", "json"], default="auto", help="json: tools described in the prompt (small models)")
    ap.add_argument("--tools", default="core", help="with --llm: core (default, short prompt) | all | comma-separated tool names")
    ap.add_argument("--timeout", type=int, default=900, help="with --llm: seconds per model call (CPU inference can be slow)")
    ap.add_argument("--mcp", nargs="?", const="bundled", metavar="CMD",
                    help="route tool calls through an MCP server over stdio: the bundled rth-mcp, or CMD")
    ap.add_argument("--allow", action="append", default=[], help="pre-authorize risk classes: writes,network,exec")
    ap.add_argument("--no-prompt", action="store_true", help="never ask interactively; deny anything not pre-authorized")
    ap.add_argument("--allow-remote-llm", action="store_true", help="permit a non-local LLM endpoint (breaks the offline guarantee)")
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("-p", "--param", action="append", default=[], help="planner mode: key=value")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    goal = " ".join(a.goal)
    allow = {x.strip() for v in a.allow for x in v.split(",") if x.strip()}
    if (a.url or a.model) and not a.llm:
        sys.exit("--url/--model need --llm ollama|openai (without --llm no model is used)")
    if a.url and not a.allow_remote_llm and not is_local_url(a.url):
        sys.exit(f"refusing non-local LLM endpoint {a.url} (offline agent); pass --allow-remote-llm to override")
    backend, note = (None, "planner (no LLM)") if not a.llm else autodetect(a.url, a.model, a.llm)
    if a.llm and backend is None:
        sys.exit(note)
    if backend:
        backend.timeout = a.timeout
    tools_filter = None if a.tools == "all" else CORE_TOOLS if a.tools == "core" else [t.strip() for t in a.tools.split(",")]
    params = {}
    for kv in a.param:
        k, _, v = kv.partition("=")
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    tools = (MCPClient(None if a.mcp == "bundled" else a.mcp.split()) if a.mcp else InProcessTools())
    try:
        agent = Agent(backend, tools, Policy(allow, interactive=False if a.no_prompt else None), max_steps=a.max_steps,
                      tool_mode=a.tool_mode, tools_filter=tools_filter if backend else None)
        via = f"MCP server {tools.server.get('name')}" if a.mcp else "in-process tools"
        print(f"[rth-agent] {note}; {via} ({len(agent.tools)} tools)", file=sys.stderr)
        out = agent.run_llm(goal) if backend else agent.run_planner(goal, params)
        out["backend"], out["tools_via"] = note, via
    finally:
        tools.close()
    if a.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        print(out["answer"])
        print("\n-- verification (checked by the harness, not the model)")
        for v in out["verification"]:
            print(f"   run {v['run']}: {v.get('overall')}" + (f"; unverified: {v['unverified_steps']}" if v.get("unverified_steps") else ""))
        print(f"-- transcript: {out['transcript']}")
    sys.exit({"verified": 0, "answered": 0, "denied": 3, "needs-approval": 3, "needs-input": 3}.get(out["status"], 1))


if __name__ == "__main__":
    main()
