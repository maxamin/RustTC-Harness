"""Programmatic API shared by the CLI and the MCP server. Every function returns JSON-serializable data."""
import json
from pathlib import Path

from . import audit, executor, matcher, planner, recover, registry, undo
from . import env as envmod
from . import ops as O

BRIEF_KEYS = ("id", "status", "tool", "error", "semantics", "truncated", "undo")


def get_env(refresh=False):
    return envmod.detect(refresh=refresh)


def summarize(res, brief=False, max_records=None):
    if max_records is not None:
        for s in res["steps"]:
            recs = executor.validate.records(s.get("data"))
            if recs is not None and len(recs) > max_records:
                if isinstance(s["data"], list):
                    s["data"] = s["data"][:max_records]
                else:
                    for k in executor.validate.RECORD_KEYS:
                        if isinstance(s["data"].get(k), list):
                            s["data"][k] = s["data"][k][:max_records]
                s["truncated"] = {"shown": max_records, "total": len(recs)}
    if not brief:
        return res
    out = {k: res[k] for k in ("run", "objective", "status", "run_dir", "remaining")}
    if res.get("warnings"):
        out["warnings"] = res["warnings"]
    out["steps"] = []
    for s in res["steps"]:
        row = {k: s.get(k) for k in BRIEF_KEYS if s.get(k) is not None}
        recs = executor.validate.records(s.get("data"))
        if recs is not None:
            row["records"] = len(recs)
        v = s.get("verification")
        if v and not v["passed"]:
            row["failed_checks"] = [c for c in v["checks"] if not c["passed"]]
        out["steps"].append(row)
    return out


def search(query, env, *, category=None, kind=None, installed=False, limit=15):
    inst = env.installed_tool_ids() if installed else None
    return [{"id": t["id"], "score": s, "binary": t["binary"], "category": t["category"], "kind": t["kind"], "status": t["status"],
             "capabilities": t["capabilities"], "purpose": t["purpose"][:140], "matched": why}
            for s, t, why in registry.search(query, category=category, installed=inst, kind=kind, limit=limit)]


def info(tool, env):
    t = registry.get(tool)
    if not t:
        raise KeyError(f"unknown tool {tool!r}; try search")
    ident = env.identity(t) if t["binary"] else {"state": "no binary (library/gui/service)"}
    return {**t, "environment": ident,
            **({"install_here": registry.install_hints(t, env, limit=5)} if ident.get("state") == "missing" else {})}


def discover(intent, env, limit=3):
    return matcher.discover(intent, env, limit=limit)


def select(intent, env):
    return matcher.select(intent, env)


def list_ops(name=None):
    if name:
        return O.describe(O.OPS[name])
    return [{"op": o.name, "capability": o.capability, "risk": o.risk, "summary": o.summary,
             "tools": [i.tool for i in o.impls], "undo": bool(o.undo)} for o in O.OPS.values()]


def plan(objective, env, params=None, workflow=None):
    return planner.plan(objective or "", env, params or {}, workflow=workflow)


def run_op(op, env, params=None, *, tool=None, timeout=None, allow=(), approve=False, dry_run=False, options=None):
    if op not in O.OPS:
        raise KeyError(f"unknown op {op!r}; valid ops: {sorted(O.OPS)}")
    p = planner.single_op_plan(f"run {op}", op, dict(params or {}))
    if tool:
        p["steps"][0]["tool"] = tool
    if timeout:
        p["steps"][0]["timeout"] = timeout
    return executor.execute(p, env, allow=allow, approve=[op] if approve else [], dry_run=dry_run, options=options)


def execute(plan_obj, env, *, allow=(), approve=(), dry_run=False, options=None, resume=None):
    """resume: a previous run id or result path; its verified, unchanged steps are reused."""
    prior = load_result(resume) if resume else None
    return executor.execute(plan_obj, env, allow=allow, approve=approve, dry_run=dry_run, options=options, resume=prior)


def load_result(ref):
    p = Path(ref)
    if p.exists():
        return json.loads(p.read_text())
    return json.loads((audit.runs_dir() / ref / "result.json").read_text())


def verify(ref):
    res = load_result(ref)
    report = []
    for s in res["steps"]:
        v = s.get("verification") or {}
        report.append({"step": s["id"], "status": s["status"], "tool": s.get("tool"),
                       "evidence": [c for c in v.get("checks", []) if c["passed"] and c["check"] != "exit_in"],
                       "failed_checks": [c for c in v.get("checks", []) if not c["passed"]],
                       "semantics": s.get("semantics"), **({"undo": s["undo"]} if s.get("undo") else {})})
    unverified = [r["step"] for r in report if r["status"] not in ("verified", "not-applicable")]
    return {"run": res["run"], "objective": res.get("objective"), "overall": res["status"],
            "all_steps_verified": res["status"] == "verified", "unverified_steps": unverified, "steps": report,
            "reminder": "Check the evidence answers the user's actual question; tool success is not task success."}


def recover_advice(tool, env, *, stderr="", exit_code=None, timed_out=False, capability=None):
    kind = recover.classify(exit_code=exit_code, stderr=stderr or "", timed_out=timed_out, missing=not env.has(tool)) or "none"
    t = registry.get(tool)
    caps = [capability] if capability else (t["capabilities"] if t else [])
    if not caps:
        caps = sorted({o.capability for o in O.OPS.values() for i in o.impls if i.tool == tool})
    alts = {c: [x for x in matcher.rank_candidates(c, env) if x["installed"] and x["binary"] != tool][:5] for c in caps}
    return {"tool": tool, "classification": recover.decision(kind), "alternatives": alts,
            "semantic_gaps": {c: registry.capabilities().get(c, {}).get("semantic_gaps") for c in caps},
            "help_hint": f"run op help with tool={tool}" if env.has(tool) else None}


def rollback(run_id, *, apply=False, force=False):
    d = audit.runs_dir() / run_id
    if not d.is_dir():
        raise KeyError(f"unknown run {run_id!r}")
    rep = undo.rollback(d, apply=apply, force=force)
    audit.AuditLog(run_id).event("rollback", applied=apply, force=force, actions=len(rep["actions"]), conflicts=rep.get("conflicts"))
    return rep


STALE_DAYS = 30


def registry_health():
    """Freshness of the upstream data and sync with the catalogue README (when it is next to the skill)."""
    import datetime as dt
    import importlib.util
    reg = json.loads((registry.REGISTRY_DIR / "tools.json").read_text())
    up_file = registry.REGISTRY_DIR / "upstream.json"
    gen = json.loads(up_file.read_text()).get("generated_at") if up_file.exists() else None
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(gen)).days if gen else None
    out = {"built_at": reg["built_at"], "upstream_fetched": gen, "upstream_age_days": age,
           "stale": age is None or age > STALE_DAYS, "refresh": "scripts/build_registry.py fetch --refresh && scripts/build_registry.py build"}
    readme = registry.SKILL_DIR.parent.parent / "CATALOGUE.md"
    if readme.exists():
        spec = importlib.util.spec_from_file_location("build_registry", registry.SKILL_DIR / "scripts" / "build_registry.py")
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)
        listed = {e["id"] for e in br.parse_readme(readme)}
        indexed = set(registry.tools())
        curated = set(json.loads((registry.REGISTRY_DIR / "curated.json").read_text())["tools"])
        out["readme"] = {"entries": len(listed), "not_in_registry": sorted(listed - indexed),
                         "removed_from_readme": sorted(indexed - listed), "missing_curated": sorted(listed - curated)}
        out["in_sync"] = not (out["readme"]["not_in_registry"] or out["readme"]["removed_from_readme"] or out["readme"]["missing_curated"])
    return out


def doctor(env):
    tools = registry.tools()
    curated = [t["id"] for t in tools.values() if t["binary"] and t["verification"]["binary_source"] == "curated"]
    return {"registry_tools": len(tools), "capabilities": len(registry.capabilities()), "ops": len(O.OPS),
            "workflows": len(planner.workflows()),
            "binary_collisions": {b: ids for b, ids in registry.binaries().items() if len(ids) > 1},
            "installed_catalogue_tools": sorted(env.installed_tool_ids()),
            "state_dir": str(envmod.state_dir()), "curated_binaries": len(curated),
            "binary_sources": {k: sum(1 for t in tools.values() if t["verification"]["binary_source"] == k) for k in ("crates.io", "Cargo.toml", "curated", "unknown")},
            "registry": registry_health()}
