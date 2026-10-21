"""Dispatcher + workflow engine: run plan steps through ranked op implementations.

Loop per step: resolve inputs -> gate on risk -> pick the first usable impl ->
run with timeout -> normalize -> verify -> on failure classify and retry,
fall back to the next impl with the same capability, or stop.
Independent read-only steps in the same dependency wave run concurrently.
"""
import concurrent.futures as cf
import copy
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import ops as O
from . import history, locks, recover, registry, sandbox, undo, validate
from .audit import AuditLog, new_run_id

GATES = {O.WRITES: "writes", O.NETWORK: "network", O.RUNS_CODE: "exec", O.PRIVILEGED: "privileged",
         "rewrites-history": "history"}
MAX_INLINE_RECORDS = 50
REF = re.compile(r"\$\{([^}]+)\}")


# ----------------------------------------------------------------------------- references

def step_paths(res):
    """Paths produced by a step: `path` fields of its records, or its `files` list."""
    data = (res or {}).get("data")
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return [str(x) for x in data["files"]]
    recs = validate.records(data)
    if recs:
        return [str(r["path"]) for r in recs if isinstance(r, dict) and r.get("path")]
    return []


def _lookup(path, ctx):
    parts = path.split(".")
    if len(parts) >= 3 and parts[0] == "steps" and parts[2] == "paths":
        cur = step_paths(ctx.get("steps", {}).get(parts[1]))
        return cur[int(parts[3])] if len(parts) > 3 and parts[3].isdigit() and int(parts[3]) < len(cur) else (None if len(parts) > 3 else cur)
    cur = ctx
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur


def resolve(value, ctx):
    """`${params.x}` / `${steps.id.data.files}`: whole-string refs keep their type."""
    if isinstance(value, str):
        m = REF.fullmatch(value)
        if m:
            return _lookup(m[1], ctx)
        return REF.sub(lambda mm: str(_lookup(mm[1], ctx) if _lookup(mm[1], ctx) is not None else ""), value)
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


# ----------------------------------------------------------------------------- process running

def run_argv(argv, *, timeout, stdin=None, cwd=None, progress=None, heartbeat=10):
    """Run with a timeout, killing the whole process group; streams output so `progress(elapsed, out_bytes,
    err_bytes)` can report every `heartbeat` seconds on long steps."""
    import threading
    t0 = time.monotonic()
    try:
        argv = sandbox.wrap(argv) if sandbox.active() else argv
    except sandbox.SandboxUnavailable as e:
        return {"exit": 126, "stdout": "", "stderr": str(e), "seconds": 0.0, "timed_out": False, "sandbox_refused": True}
    try:
        group = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                text=True, cwd=cwd, env={**os.environ, "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat"}, **group)
    except FileNotFoundError:
        return {"exit": 127, "stdout": "", "stderr": f"{argv[0]}: not found", "seconds": 0.0, "timed_out": False, "missing": True}
    except PermissionError as e:
        return {"exit": 126, "stdout": "", "stderr": str(e), "seconds": 0.0, "timed_out": False}
    bufs = {"out": [], "err": []}

    def pump(stream, key):
        for chunk in iter(lambda: stream.read(65536), ""):
            bufs[key].append(chunk)
        stream.close()

    readers = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for r in readers:
        r.start()
    if stdin is not None:
        def feed():
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=feed, daemon=True).start()
    timed_out, next_beat = False, heartbeat
    while True:
        try:
            proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            if progress and elapsed >= next_beat:
                progress(round(elapsed), sum(map(len, bufs["out"])), sum(map(len, bufs["err"])))
                next_beat += heartbeat
            if elapsed > timeout:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM) if os.name != "nt" else proc.terminate()
                    proc.wait(timeout=3)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL) if os.name != "nt" else proc.kill()
                    except ProcessLookupError:
                        pass
                    proc.wait()
                break
    for r in readers:
        r.join(timeout=5)
    return {"exit": proc.returncode, "stdout": "".join(bufs["out"]), "stderr": "".join(bufs["err"]),
            "seconds": round(time.monotonic() - t0, 3), "timed_out": timed_out}


# ----------------------------------------------------------------------------- preconditions

def _required_missing(o, inputs):
    return [k for k, v in o.params.items() if str(v).startswith("REQUIRED") and inputs.get(k) in (None, "", [])]


def _scope_ok(target, scope):
    """port-scan: target must fall inside the user-authorized scope."""
    if not scope:
        return False
    for s in re.split(r"[,\s]+", str(scope)):
        if not s:
            continue
        if s == target:
            return True
        try:
            if ipaddress.ip_address(target) in ipaddress.ip_network(s, strict=False):
                return True
        except ValueError:
            continue
    return False


def _precheck(o, inputs):
    if o.name == "port-scan" and not _scope_ok(inputs.get("target", ""), inputs.get("authorized_scope")):
        return "target is not inside authorized_scope; active scanning requires explicit authorization"
    if o.name == "archive-extract":
        d = Path(inputs["dest"])
        if d.exists() and any(d.iterdir()):
            return f"dest {d} exists and is not empty; refusing to extract over existing files"
    if o.name == "safe-delete":
        missing = [p for p in O._list(inputs["paths"]) if not os.path.lexists(p)]
        if missing:
            return f"paths do not exist: {missing}"
    return None


def resolve_binary(impl, env):
    """Actual executable for an impl: its own name, else a registry alias (Debian's fdfind, batcat)."""
    if env.has(impl.tool):
        return env.which(impl.tool)
    tool = registry.tools().get(impl.registry_id) if impl.registry_id else None
    for alias in (tool or {}).get("aliases", []) if tool and tool["binary"] == impl.tool else []:
        if env.has(alias):
            return env.which(alias)
    return None


def _impl_usable(impl, env):
    if impl.tool == "builtin":
        missing = [r for r in impl.requires if not env.has(r)]
        return ("missing-binary", f"requires {missing}") if missing else (None, None)
    if not resolve_binary(impl, env):
        return "missing-binary", f"{impl.tool} not on PATH"
    for r in impl.requires:
        if not env.has(r):
            return "missing-binary", f"requires {r}"
    if impl.registry_id:
        tool = registry.tools().get(impl.registry_id)
        if tool:
            ident = env.identity(tool)
            if ident["state"] == "conflict":
                return "identity-conflict", f"`{impl.tool}` on PATH is {ident['actually']}, not {impl.registry_id}"
    return None, None


# ----------------------------------------------------------------------------- step execution

FOREACH_MAX = 50


def run_step(step, ctx, env, log, *, allow, approve, dry_run=False, options=None):
    options = options or {}
    if step.get("foreach"):
        return _run_foreach(step, ctx, env, log, allow=allow, approve=approve, dry_run=dry_run, options=options)
    return _run_single(step, ctx, env, log, allow=allow, approve=approve, dry_run=dry_run, options=options)


def _run_foreach(step, ctx, env, log, *, allow, approve, dry_run, options):
    """Run the step once per item of `foreach` (a list or ${...} ref), binding each to `foreach_param`."""
    items = resolve(step["foreach"], ctx) or []
    items = items if isinstance(items, list) else [items]
    cap = int(step.get("foreach_max", FOREACH_MAX))
    param = step.get("foreach_param", "path")
    subs = []
    for i, item in enumerate(items[:cap]):
        sub = {k: v for k, v in step.items() if k not in ("foreach", "foreach_param", "foreach_max")}
        sub["id"] = f"{step['id']}[{i}]"
        sub["inputs"] = {**step.get("inputs", {}), param: item}
        r = _run_single(sub, ctx, env, log, allow=allow, approve=approve or {step["id"]} & set(approve), dry_run=dry_run, options=options)
        subs.append({"item": item, "status": r["status"], "tool": r.get("tool"), "data": r.get("data"), "error": r.get("error")})
    sts = {s["status"] for s in subs}
    status = ("not-applicable" if not items else "verified" if sts <= {"verified"} else
              "needs-approval" if "needs-approval" in sts else "partial" if sts & {"verified"} else "failed")
    return {"id": step["id"], "op": step["op"], "purpose": step.get("purpose"), "status": status, "foreach": {
        "param": param, "items": len(items), "ran": len(subs), "capped": len(items) > cap},
        "data": subs, "attempts": [], "step_hash": _step_hash(step),
        **({"error": f"no items to iterate (from {step['foreach']})"} if not items else {})}


def _run_single(step, ctx, env, log, *, allow, approve, dry_run=False, options=None):
    options = options or {}
    o = O.OPS.get(step["op"])
    if getattr(env, "remote", False):
        from . import remote
        if o and (o.risk in (O.WRITES, "rewrites-history") or o.name in remote.REMOTE_UNSUPPORTED_OPS):
            return {"id": step["id"], "op": step["op"], "status": "blocked", "attempts": [], "step_hash": _step_hash(step),
                    "error": f"{o.name} is not supported on remote targets (writes/local artifacts: no undo, lock or verification remotely)"}
    if o and o.risk == O.RUNS_CODE and not dry_run and not getattr(env, "remote", False):
        sandbox.configure(None if options.get("no_limits") else sandbox.DEFAULT_LIMITS,
                          isolate=options.get("sandbox", False) or options.get("sandbox_readonly", False),
                          writable=[str(log.dir)], readonly_cwd=options.get("sandbox_readonly", False))
    else:
        sandbox.clear()
    lock_paths = []
    if o and o.undo and not dry_run:
        try:
            spec = o.undo({k: v for k, v in resolve(step.get("inputs", {}), ctx).items() if v is not None})
            lock_paths = [p for key in ("modify", "create", "deleted") for p in spec.get(key, [])]
            if spec.get("git"):
                lock_paths.append(spec["git"]["repo"])  # one writer per repository
        except (KeyError, TypeError):
            lock_paths = []  # missing inputs: _run_step reports needs-input
    try:
        with locks.hold(lock_paths, log.run_id, wait=int(options.get("lock_wait", 30))):
            res = _run_step(step, ctx, env, log, allow=allow, approve=approve, dry_run=dry_run, options=options)
    except locks.Locked as e:
        res = {"id": step["id"], "op": step["op"], "status": "blocked", "error": str(e), "attempts": []}
        log.event("lock_blocked", step=step["id"], error=str(e))
    finally:
        applied = sandbox.applied()
        sandbox.clear()
    if applied:
        res["sandbox"] = applied
    res["step_hash"] = _step_hash(step)
    return res


def _step_hash(step):
    import hashlib
    return hashlib.sha256(json.dumps(step, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _run_step(step, ctx, env, log, *, allow, approve, dry_run=False, options=None):
    o = O.OPS.get(step["op"])
    res = {"id": step["id"], "op": step["op"], "purpose": step.get("purpose"), "attempts": [], "semantics": None}
    if not o:
        return {**res, "status": "failed", "error": f"unknown op {step['op']}"}
    inputs = {k: v for k, v in resolve(step.get("inputs", {}), ctx).items() if v is not None}
    res["inputs"] = inputs
    missing = _required_missing(o, inputs)
    if missing:
        return {**res, "status": "needs-input", "error": f"missing required inputs: {missing}; op params: {o.params}"}
    pre = _precheck(o, inputs)
    if pre:
        return {**res, "status": "blocked", "error": pre}
    impls = list(o.impls)
    if step.get("tool"):
        impls.sort(key=lambda i: i.tool != step["tool"] and i.registry_id != step["tool"])
    timeout = int(step.get("timeout") or o.timeout)
    on_failure = step.get("on_failure", "fallback")
    spec = {**o.verify, **(step.get("verification") or {})}
    log.event("step_start", step=step["id"], op=o.name, inputs=inputs)
    manifest = None  # undo snapshot, taken once before the first write attempt

    for impl in impls:
        risk = impl.risk or o.risk
        gate = GATES.get(risk)
        if gate and impl.risk and gate not in allow and step["id"] not in approve:
            # impl-specific risk (e.g. a privileged variant): skip it, let safer impls of the same op run
            res["attempts"].append({"tool": impl.registry_id or impl.tool, "skipped": "needs-approval", "reason": f"{risk}: needs --allow {gate}"})
            continue
        if gate and gate not in allow and step["id"] not in approve:
            res.update(status="needs-approval", risk=risk,
                       error=f"step changes state or reaches out ({risk}); rerun with --allow {gate} or --approve {step['id']}")
            log.event("gate", step=step["id"], risk=risk)
            return res
        kind, why = _impl_usable(impl, env)
        if not kind and impl.tool == "builtin" and getattr(env, "remote", False):
            kind, why = "cannot-express", "pure-Python implementation runs locally only; skipped for a remote target"
        if not kind and not options.get("no_learn"):
            why = history.learned_skip(o.name, impl.registry_id or impl.tool, env.data.get("path_hash"))
            kind = "learned-skip" if why else None
        if not kind and impl.when:
            why = impl.when(inputs, env)
            kind = "cannot-express" if why else None
        if kind:
            res["attempts"].append({"tool": impl.tool, "skipped": kind, "reason": why})
            continue
        p = copy.deepcopy(inputs)
        p["_run_dir"] = str(log.dir)
        if impl.tool == "builtin":
            argv = None
        else:
            argv = impl.build(p, env)
            if argv is None:
                res["attempts"].append({"tool": impl.tool, "skipped": "cannot-express", "reason": "parameters not expressible with this tool"})
                continue
            argv = [str(a) for a in argv]
            if argv[0] == impl.tool:
                argv[0] = resolve_binary(impl, env)
        if dry_run:
            res["attempts"].append({"tool": impl.tool, "argv": argv, "dry_run": True})
            return {**res, "status": "planned", "tool": impl.tool, "argv": argv, "semantics": impl.semantics or None}
        if o.undo and manifest is None:
            manifest = undo.snapshot(log.dir, step["id"], o.undo(p))
            res["undo"] = {"manifest": manifest, "rollback": f"rth rollback {log.run_id} --apply"}
            log.event("undo_snapshot", step=step["id"], manifest=manifest)
        if o.name == "archive-extract":
            Path(p["dest"]).mkdir(parents=True, exist_ok=True)

        for attempt_no in (1, 2):
            att = {"tool": impl.tool, "argv": argv, "try": attempt_no}
            if impl.tool == "builtin":
                t0 = time.monotonic()
                try:
                    data, err, parse_error = impl.run(p, env), "", None
                    r = {"exit": 0, "stdout": "", "stderr": "", "seconds": round(time.monotonic() - t0, 3), "timed_out": False}
                except Exception as e:  # builtin failure is a normal recoverable failure
                    data, parse_error = None, None
                    r = {"exit": 1, "stdout": "", "stderr": f"{type(e).__name__}: {e}", "seconds": round(time.monotonic() - t0, 3), "timed_out": False}
            else:
                def beat(elapsed, ob, eb, _tool=impl.tool):
                    log.event("heartbeat", step=step["id"], tool=_tool, elapsed=elapsed, stdout_bytes=ob, stderr_bytes=eb)
                    if options.get("progress"):
                        print(f"[rth] {step['id']}: {_tool} running {elapsed}s, stdout {ob}B, stderr {eb}B", file=sys.stderr, flush=True)

                if getattr(env, "remote", False):
                    from . import remote
                    att_argv = remote.wrap(env.target, argv)
                    att["target"] = env.target
                else:
                    att_argv = argv
                r = run_argv(att_argv, timeout=timeout, stdin=impl.stdin(p) if impl.stdin else None, progress=beat)
                data, parse_error = None, None
                if r["exit"] in impl.ok_exit and not r["timed_out"]:
                    try:
                        data = impl.parse(r["stdout"], p) if impl.parse else (r["stdout"].strip() or None)
                    except (ValueError, KeyError, IndexError, TypeError, OSError) as e:
                        parse_error = f"{type(e).__name__}: {e}"
            att.update(exit=r["exit"], seconds=r["seconds"], timed_out=r["timed_out"])
            if r["stdout"]:
                att["stdout_artifact"] = log.artifact(f"{step['id']}.{impl.tool}.{attempt_no}.stdout", r["stdout"])
            if r["stderr"]:
                att["stderr_tail"] = r["stderr"][-600:]
                log.artifact(f"{step['id']}.{impl.tool}.{attempt_no}.stderr", r["stderr"])
            kind = recover.classify(exit_code=r["exit"], stderr=r["stderr"], timed_out=r["timed_out"], parse_error=parse_error,
                                    ok_exit=impl.ok_exit if impl.tool != "builtin" else (0,), missing=r.get("missing", False))
            if parse_error:
                att["parse_error"] = parse_error
            if r.get("sandbox_refused"):
                kind = "sandbox-unavailable"
            log.event("attempt", step=step["id"], **{k: v for k, v in att.items() if k != "stdout_artifact"}, failure=kind)
            history.record(o.name, impl.registry_id or impl.tool, kind, env.data.get("path_hash"), r.get("seconds"))
            res["attempts"].append(att)
            if kind is None:
                ver = validate.check(spec, exit_code=r["exit"], data=data, stdout=r["stdout"], inputs=p,
                                     parsed=(parse_error is None) if (impl.parse or impl.tool == "builtin") else None)
                status = "verified" if ver["passed"] and ver["evidence"] else ("completed-unverified" if ver["passed"] else "verification-failed")
                res.update(status=status, tool=impl.tool, exit=r["exit"], verification=ver, semantics=impl.semantics or None)
                res.update(_inline(data))
                if manifest:  # before/after fingerprints are evidence that the write happened
                    eff = undo.finalize(manifest, tool=impl.tool)
                    res["effect"] = eff
                    if eff["modify_expected"]:
                        ver["checks"].append({"check": "files_modified", "passed": eff["modified"] > 0,
                                              "detail": f"{eff['modified']}/{eff['modify_expected']} files changed"})
                    if eff["create_expected"]:
                        ver["checks"].append({"check": "paths_created", "passed": eff["created"] > 0,
                                              "detail": f"{eff['created']}/{eff['create_expected']} new paths"})
                    ver["passed"] = all(c["passed"] for c in ver["checks"])
                    ver["evidence"] = ver["evidence"] or any(c["passed"] for c in ver["checks"][-2:])
                    status = "verified" if ver["passed"] and ver["evidence"] else ("completed-unverified" if ver["passed"] else "verification-failed")
                    res["status"] = status
                log.event("step_end", step=step["id"], status=status, tool=impl.tool, checks=ver["checks"])
                return res
            d = recover.decision(kind)
            att["failure"] = d["class"]
            if d["retry"] and attempt_no == 1 and (o.risk == O.READ_ONLY) and on_failure in ("retry", "fallback"):
                timeout *= 2
                continue
            break
        if on_failure == "stop" or (d["stop"] and kind != "permission-denied"):
            break
    last = next((a for a in reversed(res["attempts"]) if "failure" in a or "skipped" in a), {})
    last_tool = next((a["tool"] for a in reversed(res["attempts"]) if "exit" in a), None)
    kind = last.get("failure") or last.get("skipped") or "failed"
    if manifest:  # a failed write may have partially applied: keep rollback possible
        undo.finalize(manifest, tool=last_tool)
    res.update(status="failed", recovery=recover.decision(kind),
               error="no implementation succeeded" if res["attempts"] else "no implementation available")
    log.event("step_end", step=step["id"], status="failed", failure=kind)
    return res


def _inline(data):
    """Keep agent-facing output small; full data stays in the run directory."""
    recs = validate.records(data)
    if recs is not None and len(recs) > MAX_INLINE_RECORDS:
        if isinstance(data, list):
            return {"data": data[:MAX_INLINE_RECORDS], "truncated": {"shown": MAX_INLINE_RECORDS, "total": len(data)}}
        d = dict(data)
        for k in validate.RECORD_KEYS:
            if isinstance(d.get(k), list) and len(d[k]) > MAX_INLINE_RECORDS:
                d[k] = d[k][:MAX_INLINE_RECORDS]
        return {"data": d, "truncated": {"shown": MAX_INLINE_RECORDS, "total": len(recs)}}
    if isinstance(data, str) and len(data) > 8000:
        return {"data": data[:8000], "truncated": {"shown_chars": 8000, "total_chars": len(data)}}
    return {"data": data}


# ----------------------------------------------------------------------------- plan execution

def waves(steps):
    ids = {s["id"] for s in steps}
    done, order = set(), []
    pending = list(steps)
    while pending:
        ready = [s for s in pending if all(d in done or d not in ids for d in s.get("depends_on", []))]
        if not ready:
            raise ValueError("dependency cycle in plan: " + ", ".join(s["id"] for s in pending))
        order.append(ready)
        done |= {s["id"] for s in ready}
        pending = [s for s in pending if s["id"] not in done]
    return order


REUSABLE = ("verified", "not-applicable")


def execute(plan, env, *, allow=(), approve=(), dry_run=False, max_workers=4, options=None, resume=None):
    """resume: a previous result dict; its verified steps with unchanged definitions are reused, not rerun."""
    options = options or {}
    prior = {s["id"]: s for s in (resume or {}).get("steps", []) if s.get("status") in REUSABLE and s.get("step_hash")}
    max_age = int(options.get("resume_max_age", 3600))
    if resume and time.time() - resume.get("finished_at", 0) > max_age:
        prior = {}  # too old: the world may have changed; rerun everything
        options = {**options, "resume_note": f"run {resume.get('run')} is older than {max_age}s; nothing reused"}
    if not dry_run:
        from . import maintenance
        maintenance.maybe_auto_gc()
    if options.get("target"):
        from . import remote
        env = remote.detect(options["target"], refresh=options.get("refresh_env", False))
    run_id = new_run_id()
    log = AuditLog(run_id)
    allow, approve = set(allow), set(approve)
    ctx = {"params": plan.get("params", {}), "steps": {}}
    log.event("run_start", objective=plan.get("objective"), workflow=plan.get("workflow"), allow=sorted(allow), dry_run=dry_run,
              options=options, resume_from=(resume or {}).get("run"))
    results = []
    for wave in waves(plan["steps"]):
        runnable, blocked = [], []
        for s in wave:
            bad = [d for d in s.get("depends_on", []) if ctx["steps"].get(d, {}).get("status") not in ("verified", "completed-unverified", "planned")]
            if bad and not s.get("run_if_deps_fail"):
                blocked.append({"id": s["id"], "op": s["op"], "status": "skipped", "error": f"dependency not satisfied: {bad}"})
            elif s.get("when") and not resolve(s["when"], ctx):
                blocked.append({"id": s["id"], "op": s["op"], "status": "not-applicable", "error": f"condition false: {s['when']}"})
            else:
                runnable.append(s)
        reused = {}
        for s in list(runnable):
            ps = prior.get(s["id"])
            if ps and ps["step_hash"] == _step_hash(s):
                reused[s["id"]] = {**ps, "reused_from": resume.get("run")}
                runnable.remove(s)
                log.event("step_reused", step=s["id"], from_run=resume.get("run"))
        par = [s for s in runnable if O.OPS.get(s["op"]) and O.OPS[s["op"]].risk == O.READ_ONLY]
        seq = [s for s in runnable if s not in par]
        out = dict(reused)
        if len(par) > 1:
            with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(run_step, s, ctx, env, log, allow=allow, approve=approve, dry_run=dry_run, options=options): s for s in par}
                for f in cf.as_completed(futs):
                    out[futs[f]["id"]] = f.result()
        else:
            seq = par + seq
        for s in seq:
            out[s["id"]] = run_step(s, ctx, env, log, allow=allow, approve=approve, dry_run=dry_run, options=options)
        for r in blocked:
            out[r["id"]] = r
        for s in wave:
            r = out[s["id"]]
            if s.get("critical") is False:
                r["critical"] = False
            ctx["steps"][s["id"]] = r
            results.append(r)
        if any(out[s["id"]]["status"] in ("failed", "partial") and s.get("on_failure") == "stop" for s in wave):
            break
    # a state-changing step is proven by a later read-only check (e.g. apply <- after)
    by_id = {r["id"]: r for r in results}
    for s in plan["steps"]:
        r = by_id.get(s["id"])
        if r and s.get("verified_by") and r["status"] == "completed-unverified":
            if all(by_id.get(v, {}).get("status") == "verified" for v in s["verified_by"]):
                r["status"] = "verified"
                r.setdefault("verification", {"checks": []})["checks"].append(
                    {"check": "verified_by", "passed": True, "detail": s["verified_by"]})
    status = _overall(results, plan)
    final = {"schema": "rust-tools-harness/result@1", "run": run_id, "finished_at": time.time(),
             **({"resume_note": options["resume_note"]} if options.get("resume_note") else {}), "objective": plan.get("objective"), "workflow": plan.get("workflow"),
             "status": status, "steps": results, "run_dir": str(log.dir), "remaining": _remaining(results, plan),
             **({"warnings": plan["warnings"]} if plan.get("warnings") else {})}
    (log.dir / "result.json").write_text(json.dumps(final, indent=1, default=str))
    log.event("run_end", status=status)
    return final


def _overall(results, plan):
    crit = [r for r in results if r.get("critical", True) and r["status"] != "not-applicable"]
    st = {r["status"] for r in crit}
    if not results:
        return "empty"
    if st <= {"planned"}:
        return "planned"
    if "needs-approval" in st:
        return "needs-approval"
    if "needs-input" in st:
        return "needs-input"
    if st & {"failed", "blocked", "verification-failed", "skipped", "partial"}:
        return "partial" if st & {"verified", "completed-unverified"} else "failed"
    if st <= {"verified"}:
        return "verified"
    return "completed-unverified"


def _remaining(results, plan):
    out = []
    for r in results:
        if r["status"] in ("verified", "planned", "not-applicable"):
            continue
        out.append({"step": r["id"], "status": r["status"], "why": r.get("error") or (r.get("recovery") or {}).get("advice")})
    return out
