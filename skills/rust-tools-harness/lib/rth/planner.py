"""Planner: objective -> stateful plan (workflow template or single-op plan)."""
import json
import re
from pathlib import Path

from . import matcher
from . import ops as O
from . import registry

STEP_DEFAULTS = {"depends_on": [], "on_failure": "fallback", "critical": True}


def workflows():
    out = {}
    for f in sorted(registry.WORKFLOW_DIR.glob("*.json")):
        w = json.loads(f.read_text())
        out[w["name"]] = w
    return out


def match_workflow(objective):
    q = objective.lower().replace("_", " ")
    qw = registry.words(q) - matcher.STOP
    qw |= {w.rsplit(".", 1)[1] for w in qw if "." in w}  # "data.csv" also counts as "csv"
    best = []
    for w in workflows().values():
        score = 0.0
        strong = set(w.get("strong_keywords", []))
        for kw in set(w.get("keywords", [])) | strong:
            hit = kw in q if " " in kw else kw in qw
            if hit:
                score += (4 if kw in strong else 2) + kw.count(" ")
        if score:
            best.append((score, w["name"]))
    best.sort(reverse=True)
    return best


def _guess_params(objective, spec):
    """Cheap extraction: existing paths and quoted strings. Explicit --param always wins."""
    guessed = {}
    quoted = re.findall(r"[\"'`]([^\"'`]+)[\"'`]", objective)
    for tok in re.findall(r"(?:\./|/|~/)[^\s,;\"'`]+|\b[\w.-]+\.(?:csv|json|tsv|zip|tar|gz|tgz|zst|xz|py|rs|txt|md)\b", objective):
        p = Path(tok).expanduser()
        if p.exists():
            key = "file" if p.is_file() and "file" in spec else "archive" if p.is_file() and "archive" in spec else "path"
            guessed.setdefault(key, str(p))
    if quoted and "pattern" in spec:
        guessed.setdefault("pattern", quoted[0])
    if len(quoted) > 1 and "replacement" in spec:
        guessed.setdefault("replacement", quoted[1])
    if len(quoted) > 1 and "baseline" in spec:
        guessed.setdefault("baseline", quoted[0])
        guessed.setdefault("candidate", quoted[1])
    if quoted and "commands" in spec:
        guessed.setdefault("commands", quoted)
    if not quoted and "pattern" in spec:
        m = re.search(r"\b(?:for|named|called)\s+(\S+)\s*$", objective)
        if m:
            guessed.setdefault("pattern", m[1])
    m = re.search(r"\b(?:to|on|host|target)\s+([a-z0-9.-]+\.[a-z]{2,}|\d+\.\d+\.\d+\.\d+)\b", objective, re.I)
    if m and "target" in spec:
        guessed.setdefault("target", m[1])
    for k, v in extract_constraints(objective).items():
        if k in spec:
            guessed.setdefault(k, v)
    return guessed


LANG_EXT = {"python": "py", "rust": "rs", "javascript": "js", "typescript": "ts", "go": "go", "java": "java", "ruby": "rb",
            "markdown": "md", "shell": "sh", "bash": "sh", "c++": "cpp", "yaml": "yaml", "json": "json", "csv": "csv",
            "toml": "toml", "html": "html", "css": "css", "lua": "lua", "php": "php", "kotlin": "kt", "swift": "swift"}
SIZE_UNIT = {"b": "b", "byte": "b", "bytes": "b", "k": "k", "kb": "k", "m": "m", "mb": "m", "g": "g", "gb": "g", "t": "t", "tb": "t",
             "kib": "ki", "mib": "mi", "gib": "gi", "tib": "ti"}
TIME_UNIT = {"s": "s", "sec": "s", "second": "s", "min": "min", "minute": "min", "h": "h", "hr": "h", "hour": "h",
             "d": "d", "day": "d", "w": "w", "week": "w"}


def extract_constraints(text):
    """Plain-language constraints -> op params: sizes, time windows, file types, counts, depth, ports, durations."""
    t = text.lower()
    out = {}
    m = re.search(r"(?:bigger|larger|greater|more|over|above|at least|>=?)\s+(?:than\s+)?(\d+(?:\.\d+)?)\s*(kib|mib|gib|tib|kb|mb|gb|tb|bytes?|[kmgt])\b", t)
    if m:
        n, u = m[1], SIZE_UNIT[m[2]]
        out["min_size"] = f"{n}{u.upper() if u in 'kmgt' else u.capitalize() if u.endswith('i') else u}"
    m = re.search(r"(?:last|past|within|in the last|previous)\s+(\d+)?\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|[smhdw])\b", t)
    if m:
        unit = TIME_UNIT.get(m[2].rstrip("s") if len(m[2]) > 3 else m[2], TIME_UNIT.get(m[2][0]))
        out["changed_within"] = f"{m[1] or 1}{unit}"
    elif re.search(r"\btoday\b|\bsince yesterday\b", t):
        out["changed_within"] = "1d"
    m = re.search(r"(?:\*?\.|only\s+\.?)([a-z0-9]{1,5})\s+files?\b", t) or re.search(r"\b(" + "|".join(map(re.escape, LANG_EXT)) + r")\s+(?:source\s+)?files?\b", t)
    if m:
        out["ext"] = LANG_EXT.get(m[1], m[1])
    m = re.search(r"\b(?:top|first|largest|biggest)\s+(\d+)\b", t)
    if m:
        out["limit"] = out["top"] = int(m[1])
        out["n"] = int(m[1])
    m = re.search(r"\b(?:depth|levels?)\s*(?:of\s*)?(\d+)\b|\b(\d+)\s+levels?\b", t)
    if m:
        out["depth"] = out["max_depth"] = int(m[1] or m[2])
    m = re.search(r"\bports?\s+([\d,\-\s]+\d)", t)
    if m:
        out["ports"] = re.sub(r"\s+", "", m[1])
    m = re.search(r"\bfor\s+(\d+)\s*(?:s|sec|secs|seconds)\b", t)
    if m:
        out["seconds"] = int(m[1])
    m = re.search(r"\b(\d+)\s+runs?\b", t)
    if m:
        out["runs"] = int(m[1])
    if re.search(r"\b(?:by|using the most|most)\s+(?:memory|ram|mem)\b", t):
        out["sort"] = "mem"
    elif re.search(r"\b(?:by|using the most|most)\s+cpu\b", t):
        out["sort"] = "cpu"
    if re.search(r"\b(?:case[- ]insensitive|ignore case|ignoring case)\b", t):
        out["ignore_case"] = True
    if re.search(r"\b(?:including|include|with)\s+hidden\b", t):
        out["hidden"] = True
    if re.search(r"\b(?:including|include)\s+(?:ignored|gitignored)\b|\bno[- ]ignore\b", t):
        out["no_ignore"] = True
    for m in re.finditer(r"\b(?:in|under|inside|within)\s+(\.{0,2}/?[\w.\-/~]+)", text):
        cand = Path(m[1]).expanduser()
        if m[1] != "." and not re.fullmatch(r"\d+", m[1]) and cand.exists():
            out["path"] = str(cand)
            break
    return out


def instantiate(w, params):
    spec = w.get("params", {})
    values = {k: v.get("default") for k, v in spec.items() if isinstance(v, dict) and "default" in v}
    values.update({k: v for k, v in params.items() if v is not None})
    missing = [k for k, v in spec.items() if isinstance(v, dict) and v.get("required") and values.get(k) in (None, "")]
    if values.get("file"):
        ext = Path(str(values["file"])).suffix.lower()
        values.setdefault("is_csv", ext in (".csv", ".tsv"))
        values.setdefault("is_json", ext in (".json", ".jsonl", ".ndjson"))
    steps = [{**STEP_DEFAULTS, **s} for s in w["steps"]]
    for s in steps:
        o = O.OPS.get(s["op"])
        s.setdefault("tool", None)
        s.setdefault("timeout", o.timeout if o else 120)
        s.setdefault("preconditions", _preconditions(o))
        s.setdefault("verification", o.verify if o else {})
    return {"schema": "rust-tools-harness/plan@1", "objective": w.get("description"), "workflow": w["name"], "params": values,
            "status": "needs-input" if missing else "ready", "missing_params": missing or None,
            "notes": w.get("notes"), "steps": steps}


def _preconditions(o):
    if not o:
        return {}
    return {"any_of_tools": [i.tool for i in o.impls], "risk": o.risk,
            "approval": None if o.risk == O.READ_ONLY else f"--allow {o.risk} or --approve <step-id>"}


def single_op_plan(objective, op_name, params):
    o = O.OPS[op_name]
    params, warnings = normalize_params(o.params, params)
    missing = [k for k, v in o.params.items() if str(v).startswith("REQUIRED") and params.get(k) in (None, "")]
    return {"schema": "rust-tools-harness/plan@1", "objective": objective, "workflow": None, "params": params,
            "status": "needs-input" if missing else "ready", "missing_params": missing or None,
            **({"warnings": warnings} if warnings else {}),
            "steps": [{**STEP_DEFAULTS, "id": op_name, "purpose": o.summary, "op": op_name, "tool": None,
                       "inputs": {k: f"${{params.{k}}}" for k in o.params}, "timeout": o.timeout,
                       "preconditions": _preconditions(o), "expected_output": o.summary,
                       "verification": o.verify}]}


ALIASES = {"pattern": ["identifier", "query", "regex", "term", "text", "search", "symbol", "needle", "string"],
           "path": ["dir", "directory", "folder", "root", "cwd", "repo", "location"],
           "file": ["file_path", "filename", "filepath", "input"], "replacement": ["replace", "new", "with", "to"],
           "commands": ["command", "cmd", "cmds"], "target": ["host", "hostname", "address", "ip"],
           "limit": ["max", "max_results", "count", "top_n"]}


def normalize_params(spec, params):
    """Map common synonyms onto real param names (small models guess names). Returns (params, warnings)."""
    out, warnings = {}, []
    for k, v in (params or {}).items():
        if k in spec:
            out[k] = v
            continue
        real = next((r for r, al in ALIASES.items() if k.lower() in al and r in spec), None)
        if real and real not in params:
            out[real] = v
            warnings.append(f"param {k!r} interpreted as {real!r}")
        else:
            warnings.append(f"ignored unknown param {k!r}; valid params: {sorted(spec)}")
    return out, warnings


def plan(objective, env, params=None, workflow=None):
    params = dict(params or {})
    if workflow and workflow not in workflows():
        raise ValueError(f"unknown workflow {workflow!r}. Valid workflows: {sorted(workflows())}. "
                         "Or omit 'workflow' and let the planner choose from the objective.")
    if workflow:
        w = workflows()[workflow]
        return instantiate(w, {**_guess_params(objective or "", w.get("params", {})), **params}) | ({"objective": objective} if objective else {})
    ranked = match_workflow(objective)
    if ranked and ranked[0][0] >= 4:
        w = workflows()[ranked[0][1]]
        p = instantiate(w, {**_guess_params(objective, w.get("params", {})), **params})
        p["objective"] = objective
        p["matched_workflow_score"] = ranked[0][0]
        return p
    sel = matcher.select(objective, env)
    if not sel.get("selection"):
        return {"schema": "rust-tools-harness/plan@1", "objective": objective, "status": "no-plan",
                "note": "no workflow or executable op matches; use `rth discover` and compose manually", "discover": matcher.discover(objective, env)}
    op_name = sel["selection"]["op"]
    p = single_op_plan(objective, op_name, {**_guess_params(objective, O.OPS[op_name].params), **params})
    p["alternatives"] = [a["op"] for a in sel.get("alternatives", [])]
    return p
