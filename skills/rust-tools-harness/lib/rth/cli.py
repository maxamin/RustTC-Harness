"""rth — rust-tools-harness command line. JSON on stdout for every command with --json; run/execute/plan always emit JSON."""
import argparse
import json
import sys
from pathlib import Path

from . import api, audit, planner, registry
from . import env as envmod
from . import ops as O


def emit(obj, as_json=True):
    if as_json:
        print(json.dumps(obj, indent=1, default=str, ensure_ascii=False))
    else:
        print(obj)


def parse_params(pairs, json_params=None):
    out = json.loads(json_params) if json_params else {}
    for kv in pairs or []:
        k, sep, v = kv.partition("=")
        if not sep:
            sys.exit(f"--param expects key=value, got {kv!r}")
        try:
            out[k] = json.loads(v)  # numbers, bools, lists
        except ValueError:
            out[k] = v
    return out


def _allow(a):
    vals = set()
    for x in a.allow or []:
        vals |= {v.strip() for v in x.split(",") if v.strip()}
    return vals


def cmd_capabilities(a, env):
    caps = registry.capabilities()
    rows = []
    for cid, c in sorted(caps.items()):
        tools = registry.tools_for_capability(cid)
        installed = [t["id"] for t in tools if env.identity(t)["state"] in ("confirmed", "unconfirmed")] if not a.fast else None
        if not a.all and not tools and not c.get("native"):
            continue
        rows.append({"capability": cid, "description": c["description"], "ops": [o.name for o in O.ops_for_capability(cid)],
                     "tools": [t["id"] for t in tools], **({"installed": installed} if installed is not None else {}),
                     "native": c.get("native", [])})
    if a.executable:
        rows = [r for r in rows if r["ops"]]
    if a.json:
        return emit(rows)
    for r in rows:
        inst = f" installed={','.join(r['installed'])}" if r.get("installed") else ""
        ops = f" ops={','.join(r['ops'])}" if r["ops"] else ""
        print(f"{r['capability']:<20} {r['description'][:60]:<60}{ops}{inst}")


def cmd_search(a, env):
    rows = api.search(" ".join(a.query), env, category=a.category, kind=a.kind, installed=a.installed, limit=a.limit)
    if a.json:
        return emit(rows)
    for r in rows:
        print(f"{r['score']:>5}  {r['id']:<18} [{r['status']}/{r['kind']}] bin={r['binary'] or '-'}  {r['purpose'][:90]}")


def cmd_info(a, env):
    try:
        emit(api.info(a.tool, env))
    except KeyError as e:
        sys.exit(str(e))


def cmd_env(a, env):
    if a.versions:
        for b in sorted(env.data["binaries"]):
            env.version(b)
    out = env.summary()
    if a.versions:
        out["versions"] = env.data.get("versions")
    emit(out)


def cmd_discover(a, env):
    emit(api.discover(" ".join(a.intent), env, limit=a.limit))


def cmd_select(a, env):
    emit(api.select(" ".join(a.intent), env))


def cmd_ops(a, env):
    rows = api.list_ops(a.name)
    if a.name or a.json:
        return emit(rows)
    for r in rows:
        print(f"{r['op']:<16} {r['risk']:<15} {'/'.join(r['tools']):<32} {r['summary']}{'  [undo]' if r['undo'] else ''}")


def cmd_workflows(a, env):
    ws = planner.workflows()
    if a.name:
        return emit(ws[a.name])
    for w in ws.values():
        req = [k for k, v in w.get("params", {}).items() if isinstance(v, dict) and v.get("required")]
        print(f"{w['name']:<20} {w['description'][:80]}  required={req or '-'}")


def cmd_plan(a, env):
    p = api.plan(" ".join(a.objective), env, parse_params(a.param, a.params_json), workflow=a.workflow)
    if a.output:
        Path(a.output).write_text(json.dumps(p, indent=1, default=str))
        emit({"plan_written": a.output, "status": p.get("status"), "workflow": p.get("workflow"),
              "steps": [s["id"] for s in p.get("steps", [])], "missing_params": p.get("missing_params")})
    else:
        emit(p)


def _options(a):
    return {k: True for k in ("sandbox", "no_limits", "no_learn", "progress") if getattr(a, k, False)}


def _exit(res):
    sys.exit(0 if res["status"] in ("verified", "planned") else 3 if res["status"] in ("needs-approval", "needs-input") else 1)


def cmd_execute(a, env):
    plan = json.loads(Path(a.plan).read_text()) if a.plan != "-" else json.load(sys.stdin)
    if plan.get("status") == "needs-input" and not a.force:
        sys.exit(f"plan needs input: {plan.get('missing_params')} (set with rth plan ... --param k=v)")
    res = api.execute(plan, env, allow=_allow(a), approve=a.approve or [], dry_run=a.dry_run, options=_options(a), resume=a.resume)
    emit(api.summarize(res, a.brief, a.max_records))
    _exit(res)


def cmd_run(a, env):
    params = parse_params(a.param, a.params_json)
    if a.stdin_param:
        params[a.stdin_param] = sys.stdin.read()
    try:
        res = api.run_op(a.op, env, params, tool=a.tool, timeout=a.timeout, allow=_allow(a), approve=a.approve, dry_run=a.dry_run,
                         options=_options(a))
    except KeyError as e:
        sys.exit(f"{e}; see `rth ops`")
    emit(api.summarize(res, a.brief, a.max_records))
    _exit(res)


def cmd_verify(a, env):
    emit(api.verify(a.result))


def cmd_recover(a, env):
    emit(api.recover_advice(a.tool, env, stderr=a.stderr, exit_code=a.exit, timed_out=a.timeout, capability=a.capability))


def cmd_rollback(a, env):
    try:
        rep = api.rollback(a.run, apply=a.apply, force=a.force)
    except KeyError as e:
        sys.exit(str(e))
    emit(rep)
    sys.exit(1 if rep.get("conflicts") else 0)


def cmd_audit(a, env):
    if a.run:
        return emit(audit.read(a.run))
    emit(audit.list_runs(a.limit))


def cmd_history(a, env):
    from . import history
    rows = history.stats()
    if a.op:
        rows = [r for r in rows if r["op"] == a.op]
    if a.json:
        return emit(rows)
    for r in rows:
        skip = history.learned_skip(r["op"], r["tool"], env.data.get("path_hash"))
        print(f"{r['op']:<16} {r['tool']:<14} {r['ok']:>3}/{r['attempts']:<3} ok  median {r['median_seconds']}s  "
              f"{r['failures'] or ''}{'  SKIPPED: ' + skip if skip else ''}")


def cmd_doctor(a, env):
    emit(api.doctor(env))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # Opt-in front ends, dispatched before the harness parser: nothing starts a server or a model unless asked.
    if argv[:1] == ["mcp"]:
        from .mcp import serve
        return serve()
    if argv[:1] == ["agent"]:
        from .agent import main as agent_main
        return agent_main(argv[1:])
    ap = argparse.ArgumentParser(prog="rth", description="Rust tools harness: discover, select, plan, execute, verify, recover. "
                                 "Optional front ends: `rth agent <goal> [--llm ollama|openai] [--mcp]`, `rth mcp` (stdio MCP server).")
    ap.add_argument("--json", action="store_true", help="JSON output for list-style commands")
    ap.add_argument("--refresh-env", action="store_true", help="re-detect the environment")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("capabilities", help="list capabilities with ops, catalogue tools and native fallbacks")
    s.add_argument("--all", action="store_true"); s.add_argument("--executable", action="store_true", help="only ones with ops")
    s.add_argument("--fast", action="store_true", help="skip installed-version probes")
    s = sub.add_parser("search", help="search the tool registry"); s.add_argument("query", nargs="+")
    s.add_argument("--category"); s.add_argument("--kind"); s.add_argument("--installed", action="store_true"); s.add_argument("--limit", type=int, default=15)
    s = sub.add_parser("info", help="full registry record + local availability"); s.add_argument("tool")
    s = sub.add_parser("env", help="environment facts"); s.add_argument("--versions", action="store_true")
    s = sub.add_parser("discover", help="intent -> capabilities -> ranked tools"); s.add_argument("intent", nargs="+"); s.add_argument("--limit", type=int, default=3)
    s = sub.add_parser("select", help="intent -> best executable op/tool here"); s.add_argument("intent", nargs="+")
    s = sub.add_parser("ops", help="list executable ops (or describe one)"); s.add_argument("name", nargs="?")
    s = sub.add_parser("workflows", help="list workflow templates (or show one)"); s.add_argument("name", nargs="?")
    for name in ("plan", "run"):
        s = sub.add_parser(name, help="build a plan from an objective" if name == "plan" else "execute one op directly")
        if name == "plan":
            s.add_argument("objective", nargs="*"); s.add_argument("--workflow"); s.add_argument("-o", "--output")
        else:
            s.add_argument("op"); s.add_argument("--tool", help="preferred implementation"); s.add_argument("--timeout", type=int)
            s.add_argument("--stdin-param", help="read stdin into this param (e.g. stdin for json-query)")
            s.add_argument("--allow", action="append"); s.add_argument("--approve", action="store_true")
            s.add_argument("--dry-run", action="store_true"); s.add_argument("--brief", action="store_true")
            s.add_argument("--max-records", type=int, help="cap inline records per step")
            s.add_argument("--sandbox", action="store_true", help="isolate user-code steps with bubblewrap (no network, read-only root)")
            s.add_argument("--no-limits", action="store_true", help="disable default CPU/memory/file-size limits on user-code steps")
            s.add_argument("--no-learn", action="store_true", help="ignore learned tool failures from past runs")
            s.add_argument("--progress", action="store_true", help="print heartbeats for long-running steps to stderr")
        s.add_argument("--param", "-p", action="append", help="key=value (value parsed as JSON when possible)")
        s.add_argument("--params-json")
    s = sub.add_parser("execute", help="run a plan file"); s.add_argument("plan")
    s.add_argument("--allow", action="append", help="writes,network,exec,privileged"); s.add_argument("--approve", action="append", help="step id")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--brief", action="store_true"); s.add_argument("--force", action="store_true")
    s.add_argument("--max-records", type=int, help="cap inline records per step")
    s.add_argument("--resume", metavar="RUN", help="reuse verified, unchanged steps from a previous run")
    s.add_argument("--sandbox", action="store_true", help="isolate user-code steps with bubblewrap (no network, read-only root)")
    s.add_argument("--no-limits", action="store_true", help="disable default CPU/memory/file-size limits on user-code steps")
    s.add_argument("--no-learn", action="store_true", help="ignore learned tool failures from past runs")
    s.add_argument("--progress", action="store_true", help="print heartbeats for long-running steps to stderr")
    s = sub.add_parser("verify", help="evidence report for a result (file or run id)"); s.add_argument("result")
    s = sub.add_parser("recover", help="classify a failure and list installed alternatives"); s.add_argument("--tool", required=True)
    s.add_argument("--stderr"); s.add_argument("--exit", type=int); s.add_argument("--timeout", action="store_true"); s.add_argument("--capability")
    s = sub.add_parser("audit", help="list runs or show one run's audit log"); s.add_argument("run", nargs="?"); s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("history", help="per op/tool outcomes on this machine (what the harness has learned)"); s.add_argument("op", nargs="?")
    s = sub.add_parser("rollback", help="undo a run's write steps (dry run unless --apply)"); s.add_argument("run")
    s.add_argument("--apply", action="store_true"); s.add_argument("--force", action="store_true", help="overwrite files changed after the run")
    sub.add_parser("doctor", help="registry/environment sanity report")

    a = ap.parse_args(argv)
    env = envmod.detect(refresh=a.refresh_env)
    {"capabilities": cmd_capabilities, "search": cmd_search, "info": cmd_info, "env": cmd_env, "discover": cmd_discover,
     "select": cmd_select, "ops": cmd_ops, "workflows": cmd_workflows, "plan": cmd_plan, "run": cmd_run, "execute": cmd_execute,
     "verify": cmd_verify, "recover": cmd_recover, "rollback": cmd_rollback, "history": cmd_history, "audit": cmd_audit, "doctor": cmd_doctor}[a.cmd](a, env)


if __name__ == "__main__":
    main()
