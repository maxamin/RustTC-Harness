"""MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing the harness as structured tools.

State-changing work is split into separate *_approved tools so the MCP client's own permission
prompts apply: allowlist the read-only tools, and keep the approved ones on "ask".
"""
import json
import sys
import traceback

from . import api

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "rust-tools-harness", "version": "1.2.0"}
ALLOW_VALUES = ["writes", "network", "exec", "privileged"]

RO = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
PARAMS = {"type": "object", "description": "op parameters (see rth_ops)", "additionalProperties": True}
ALLOW = {"type": "array", "items": {"type": "string", "enum": ALLOW_VALUES},
         "description": "risk classes the user explicitly authorized"}
SANDBOX = {**B, "description": "isolate user-code steps with bubblewrap (no network, read-only root)"}
OUT = {"brief": {**B, "description": "summary without data (default true)"},
       "max_records": {**I, "description": "cap inline records per step (default 20)"}}

TOOLS = [
    ("rth_env", "Environment facts: OS, shell, privileges, runtimes, installed catalogue and native binaries.",
     _obj({"refresh": B}), RO),
    ("rth_search", "Search the 174-tool Rust catalogue by words.",
     _obj({"query": S, "installed": B, "category": S, "kind": S, "limit": I}, ["query"]), RO),
    ("rth_info", "Full registry record for one tool (flags, exit behaviour, limitations, provenance) plus local identity.",
     _obj({"tool": S}, ["tool"]), RO),
    ("rth_discover", "Intent -> matching capabilities -> ranked catalogue and native tools, with semantic gaps.",
     _obj({"intent": S, "limit": I}, ["intent"]), RO),
    ("rth_select", "Intent -> best executable op and the tool it will use on this machine.",
     _obj({"intent": S}, ["intent"]), RO),
    ("rth_ops", "List executable ops, or describe one (params, risk, implementations).", _obj({"name": S}), RO),
    ("rth_plan", "Build a plan from an objective or a named workflow. Returns the plan JSON to pass to rth_execute.",
     _obj({"objective": S, "workflow": S, "params": PARAMS}), RO),
    ("rth_run", "Run one op. Read-only: state-changing ops return status needs-approval (use rth_run_approved).",
     _obj({"op": S, "params": PARAMS, "tool": S, "timeout": I, "dry_run": B, **OUT}, ["op"]), RO),
    ("rth_execute", "Execute a plan. Read-only: gated steps stop with needs-approval (use rth_execute_approved). "
                    "resume: a previous run id whose verified, unchanged steps are reused.",
     _obj({"plan": {"type": "object"}, "dry_run": B, "resume": S, **OUT}, ["plan"]), RO),
    ("rth_run_approved", "Run one op with the risk classes the USER authorized (writes/network/exec). Write ops keep an undo snapshot.",
     _obj({"op": S, "params": PARAMS, "allow": ALLOW, "tool": S, "timeout": I, "sandbox": SANDBOX, **OUT}, ["op", "allow"]), WRITE),
    ("rth_execute_approved", "Execute a plan with user-authorized risk classes and/or specific approved step ids.",
     _obj({"plan": {"type": "object"}, "allow": ALLOW, "approve": {"type": "array", "items": S}, "sandbox": SANDBOX,
           "resume": S, **OUT}, ["plan"]), WRITE),
    ("rth_verify", "Evidence report for a run id.", _obj({"run": S}, ["run"]), RO),
    ("rth_recover", "Classify a failure and list installed alternatives with the same capability.",
     _obj({"tool": S, "stderr": S, "exit_code": I, "timed_out": B, "capability": S}, ["tool"]), RO),
    ("rth_rollback", "Preview undo of a run's write steps (dry run). Apply with rth_rollback_apply.",
     _obj({"run": S}, ["run"]), RO),
    ("rth_rollback_apply", "Undo a run's write steps: restore modified files, move created paths aside (all-or-nothing).",
     _obj({"run": S, "force": B}, ["run"]), WRITE),
    ("rth_audit", "List recent runs, or one run's audit events.", _obj({"run": S, "limit": I}), RO),
    ("rth_history", "Per op/tool success rates on this machine and tools the harness now skips (learned failures).",
     _obj({"op": S}), RO),
]


def _out(res, a):
    return api.summarize(res, a.get("brief", True), a.get("max_records", 20))


def call(name, a):
    env = api.get_env(refresh=bool(a.get("refresh")))
    if name == "rth_env":
        return env.summary()
    if name == "rth_search":
        return api.search(a["query"], env, category=a.get("category"), kind=a.get("kind"),
                          installed=a.get("installed", False), limit=a.get("limit", 15))
    if name == "rth_info":
        return api.info(a["tool"], env)
    if name == "rth_discover":
        return api.discover(a["intent"], env, limit=a.get("limit", 3))
    if name == "rth_select":
        return api.select(a["intent"], env)
    if name == "rth_ops":
        return api.list_ops(a.get("name"))
    if name == "rth_plan":
        return api.plan(a.get("objective", ""), env, a.get("params"), workflow=a.get("workflow"))
    if name == "rth_run":
        return _out(api.run_op(a["op"], env, a.get("params"), tool=a.get("tool"), timeout=a.get("timeout"),
                               dry_run=a.get("dry_run", False)), a)
    if name == "rth_execute":
        return _out(api.execute(a["plan"], env, dry_run=a.get("dry_run", False), resume=a.get("resume")), a)
    if name == "rth_run_approved":
        return _out(api.run_op(a["op"], env, a.get("params"), tool=a.get("tool"), timeout=a.get("timeout"),
                               allow=set(a.get("allow", [])), options={"sandbox": bool(a.get("sandbox"))}), a)
    if name == "rth_execute_approved":
        return _out(api.execute(a["plan"], env, allow=set(a.get("allow", [])), approve=set(a.get("approve", [])),
                                options={"sandbox": bool(a.get("sandbox"))}, resume=a.get("resume")), a)
    if name == "rth_verify":
        return api.verify(a["run"])
    if name == "rth_recover":
        return api.recover_advice(a["tool"], env, stderr=a.get("stderr", ""), exit_code=a.get("exit_code"),
                                  timed_out=a.get("timed_out", False), capability=a.get("capability"))
    if name == "rth_rollback":
        return api.rollback(a["run"], apply=False)
    if name == "rth_rollback_apply":
        return api.rollback(a["run"], apply=True, force=a.get("force", False))
    if name == "rth_history":
        from . import history
        rows = [r for r in history.stats() if not a.get("op") or r["op"] == a["op"]]
        for r in rows:
            r["learned_skip"] = history.learned_skip(r["op"], r["tool"], env.data.get("path_hash"))
        return rows
    if name == "rth_audit":
        from . import audit
        return audit.read(a["run"]) if a.get("run") else audit.list_runs(a.get("limit", 10))
    raise KeyError(f"unknown tool {name}")


INSTRUCTIONS = ("Request capabilities, not commands. rth_select/rth_plan first; read-only tools never change state; "
                "use *_approved tools only for actions the user authorized; check rth_verify evidence before reporting success.")


def tool_list():
    return [{"name": n, "description": d, "inputSchema": s, "annotations": ann} for n, d, s, ann in TOOLS]


def call_text(name, arguments):
    """Run one tool; returns (is_error, text) exactly as the MCP server would report it."""
    try:
        return False, json.dumps(call(name, arguments or {}), default=str, ensure_ascii=False)
    except (KeyError, ValueError, FileNotFoundError, TypeError) as e:
        return True, f"{type(e).__name__}: {e}"


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification (initialized, cancelled, ...): no response
        return None
    try:
        if method == "initialize":
            v = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
            result = {"protocolVersion": v, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "tools/call":
            p = msg.get("params") or {}
            is_err, text = call_text(p.get("name"), p.get("arguments"))
            result = {"content": [{"type": "text", "text": text}], "isError": is_err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # never crash the server on one bad request
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}",
                                                        "data": traceback.format_exc()[-1500:]}}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (handle(m) for m in batch) if r is not None]
        for r in replies:
            stdout.write(json.dumps(r, default=str) + "\n")
        stdout.flush()


def main():
    serve()


if __name__ == "__main__":
    main()
