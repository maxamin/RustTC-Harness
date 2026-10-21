"""Capability matcher + tool selection: intent text -> capabilities -> ranked, available tools."""
from . import ops as O
from . import registry

STOP = set("a an the and or of to in on for with by from my me i we is are be it this that what which how do does all any "
           "some get find me please can you using use".split())


SYNONYMS = {"sluggish": "slow", "laggy": "slow", "lagging": "slow", "huge": "large", "biggest": "large", "bigger": "large",
            "largest": "large", "occurrence": "usage", "occurrences": "usage", "uses": "usage", "references": "usage",
            "folder": "directory", "folders": "directory", "dirs": "directory", "dir": "directory", "programs": "process",
            "program": "process", "apps": "process", "hog": "process", "ram": "memory", "mem": "memory",
            "zip": "archive", "tarball": "archive", "tar": "archive", "unzip": "extract", "erase": "delete", "rm": "delete",
            "syscalls": "syscall", "speed": "fast", "swap": "replace", "substitute": "replace", "rewrite": "replace", "quicker": "faster", "loc": "lines"}


EXT_HINTS = {"csv": "csv", "tsv": "csv", "json": "json", "jsonl": "json", "zip": "archive", "tar": "archive", "gz": "archive",
             "tgz": "archive", "zst": "archive", "xz": "archive", "7z": "archive", "py": "python", "rs": "rust", "md": "markdown",
             "log": "log", "svg": "flamegraph"}


def stem(w):
    w = SYNONYMS.get(w, w)
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            base = w[: -len(suf)] + rep
            if suf in ("ing", "ed") and len(base) > 3 and base[-1] == base[-2] and base[-1] not in "aeiouls":
                base = base[:-1]  # committed -> commit, mapped -> map
            return SYNONYMS.get(base, base)
    return w


def stems(text):
    words = registry.words(text.lower().replace("_", " "))
    out = {stem(w) for w in words}
    for w in words:  # file names hint at the capability: backup.tar.gz -> archive, sales.csv -> csv
        for part in w.split(".")[1:]:
            if part in EXT_HINTS:
                out.add(EXT_HINTS[part])
    return out - {stem(s) for s in STOP}


# op-level keywords: break ties between ops that share a capability (csv-profile vs csv-query vs csv-frequency)
OP_KEYWORDS = {
    "csv-query": ["sql", "query", "select", "where", "group by"], "csv-frequency": ["most common", "frequency", "value counts", "top values", "common"],
    "csv-profile": ["profile", "statistics", "stats", "summary", "types"], "csv-select": ["columns only", "project", "pick columns"],
    "disk-free": ["full", "free space", "capacity", "mounted"], "dir-size": ["total size", "how big is"],
    "python-profile": ["python"], "cpu-profile": ["flamegraph", "flame", "perf", "binary", "rust"],
    "git-log": ["history", "log", "commits"], "git-diff-stat": ["changed", "diff stat"], "git-status": ["status", "uncommitted"],
    "archive-list": ["inside", "contents", "list"], "archive-extract": ["extract", "unzip", "unpack"],
    "calculate": ["convert", "calculate", "compute"], "trace-route": ["route", "traceroute", "hops", "packet loss"],
    "process-list": ["process", "memory", "cpu", "running"], "view-file": ["show", "print", "lines of file"],
}


def match_capabilities(intent, limit=5):
    q = intent.lower().replace("_", " ")
    qw = registry.words(q) - STOP
    qs = stems(q)
    scored = []
    for cid, c in registry.capabilities().items():
        score, why = 0.0, []
        for kw in c.get("keywords", []):
            if " " in kw and kw in q:
                score += 3 + kw.count(" ")
                why.append(kw)
            elif kw in qw:
                score += 2
                why.append(kw)
            elif " " in kw and len(stems(kw)) >= 2 and stems(kw) <= qs:
                score += 2 + 0.5 * kw.count(" ")
                why.append("~" + kw)
            elif " " not in kw and stem(kw) in qs:
                score += 1.5
                why.append("~" + kw)
        desc_overlap = qw & (registry.words(c["description"]) - STOP)
        if desc_overlap:
            score += 0.75 * len(desc_overlap)
            why += sorted(desc_overlap)
        if cid.replace("-", " ") in q:
            score += 3
        if O.ops_for_capability(cid):
            score += 0.5 if score else 0  # tie-break toward capabilities the harness can execute
        if score > 0:
            scored.append({"capability": cid, "score": round(score, 2), "matched": sorted(set(why))[:6], "description": c["description"]})
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


def rank_candidates(cap, env, *, interactive_ok=None):
    """Score catalogue tools and native fallbacks for one capability in this environment."""
    interactive_ok = env.data["tty"] if interactive_ok is None else interactive_ok
    op_tools = {i.registry_id or i.tool for o in O.ops_for_capability(cap) for i in o.impls}
    cands = []
    for t in registry.tools_for_capability(cap):
        ident = env.identity(t) if t["binary"] else {"state": "missing"}
        installed = ident["state"] in ("confirmed", "unconfirmed")
        score, why = 0.0, []
        if installed:
            score += 5 if ident["state"] == "confirmed" else 3  # unconfirmed: same name, unproven identity
            why.append(f"installed ({ident['state']})")
        elif ident["state"] == "conflict":
            why.append(f"name collision: PATH has {ident['actually']}")
        auto = registry.AUTOMATION_SCORE.get(t["automation"], 1)
        if t["interactive"] == "required" and not interactive_ok:
            auto = 0
            why.append("interactive-only, no TTY")
        score += auto
        if t["structured_output"]:
            score += 1
            why.append("structured output")
        if t["id"] in op_tools:
            score += 2
            why.append("harness adapter")
        st = registry.STATUS_PENALTY.get(t["status"], 0)
        if st:
            why.append(t["status"])
        score += st
        if t["risk"] not in ("read-only",) and not t["risk"].startswith("read-only"):
            why.append(f"risk: {t['risk']}")
        if t["kind"] in ("library", "gui", "service", "agent"):
            score -= 3
            why.append(f"kind: {t['kind']}")
        cand = {"tool": t["id"], "binary": t["binary"], "source": "catalogue", "installed": installed, "score": round(score, 2),
                "why": why, "status": t["status"], "automation": t["automation"]}
        if not installed and t["binary"] and t["kind"] in ("cli", "tui", "shell"):
            cand["install"] = registry.install_hints(t, env, limit=2)
        cands.append(cand)
    for n in registry.capabilities().get(cap, {}).get("native", []):
        b = n.split()[0]
        installed = env.has(b)
        score = (5 if installed else 0) + 1.5 + (2 if b in op_tools else 0)
        cands.append({"tool": n, "binary": b, "source": "native", "installed": installed, "score": score,
                      "why": ["installed" if installed else "not installed"] + (["harness adapter"] if b in op_tools else [])})
    cands.sort(key=lambda c: (-c["score"], c["tool"]))
    return cands


def discover(intent, env, limit=4, per_cap=6):
    out = []
    for m in match_capabilities(intent, limit):
        cands = rank_candidates(m["capability"], env)
        gaps = registry.capabilities()[m["capability"]].get("semantic_gaps", [])
        out.append({**m, "ops": [o.name for o in O.ops_for_capability(m["capability"])],
                    "candidates": cands[:per_cap], "semantic_gaps": gaps or None})
    return out


def select(intent, env):
    """Best executable choice: the op whose first usable impl is available here."""
    from .executor import _impl_usable  # local import avoids a cycle
    picks = []
    for m in match_capabilities(intent, 5):
        for o in O.ops_for_capability(m["capability"]):
            usable = []
            for i in o.impls:
                kind, why = _impl_usable(i, env)
                if not kind and i.when:
                    try:  # same applicability rule the executor applies, with default params
                        why = i.when({}, env)
                    except (KeyError, TypeError):
                        why = None
                    kind = "cannot-express" if why else None
                usable.append({"tool": i.tool, "usable": kind is None, **({"reason": why} if why else {})})
            first = next((u for u in usable if u["usable"]), None)
            preferred = next((i for i in o.impls if i.registry_id), None)
            upgrade = None
            if preferred and first and first["tool"] != preferred.tool and not any(u["usable"] for u in usable if u["tool"] == preferred.tool):
                t = registry.tools().get(preferred.registry_id)
                hints = registry.install_hints(t, env, limit=1) if t else []
                if hints:
                    upgrade = {"tool": preferred.tool, "why": "preferred implementation is not installed", "install": hints[0]}
            kw_bonus = sum(1.5 if (" " in k and k in intent.lower()) or (" " not in k and stem(k) in stems(intent)) else 0
                           for k in OP_KEYWORDS.get(o.name, []))
            picks.append({"capability": m["capability"], "score": round(m["score"] + kw_bonus, 2), "op": o.name, "summary": o.summary, "risk": o.risk,
                          "selected_tool": first["tool"] if first else None, "implementations": usable, "params": o.params,
                          **({"upgrade": upgrade} if upgrade else {})})
    if not picks:
        caps = match_capabilities(intent, 3)
        return {"intent": intent, "selection": None, "note": "no executable op; see `discover` for catalogue tools", "capabilities": caps}
    # relevance first (a runnable but irrelevant op answers a different question); among comparably relevant
    # ops prefer one that can run here, then one that changes nothing
    def adj(p):
        return p["score"] - (0 if p["risk"] == O.READ_ONLY else 1)
    top = max(picks, key=adj)
    near = [p for p in picks if adj(p) >= 0.7 * adj(top)]
    best = max(near, key=lambda p: (p["selected_tool"] is not None, adj(p)))
    if best["selected_tool"] is None:
        impl = next((i for i in O.OPS[best["op"]].impls if i.registry_id), None)
        tool = registry.tools().get(impl.registry_id) if impl else None
        best["not_runnable_here"] = {"install": registry.install_hints(tool, env, limit=2) if tool else [],
                                     "requires": sorted({i.tool for i in O.OPS[best["op"]].impls} | {r for i in O.OPS[best["op"]].impls for r in i.requires})}
    req = {k: f"<{v.split(' ', 1)[1] if ' ' in v else 'value'}>" for k, v in best["params"].items() if str(v).startswith("REQUIRED")}
    tool = "rth_run" if best["risk"] == O.READ_ONLY else "rth_run_approved"
    nxt = {"tool": tool, "arguments": {"op": best["op"], "params": {**req, "...": "optional params from 'params'"}}}
    return {"intent": intent, "selection": best, "next": nxt, "alternatives": [p for p in picks if p is not best][:4]}
