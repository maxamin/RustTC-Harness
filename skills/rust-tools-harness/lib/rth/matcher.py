"""Capability matcher + tool selection: intent text -> capabilities -> ranked, available tools."""
from . import ops as O
from . import registry

STOP = set("a an the and or of to in on for with by from my me i we is are be it this that what which how do does all any "
           "some show get find me please can you using use".split())


def match_capabilities(intent, limit=5):
    q = intent.lower().replace("_", " ")
    qw = registry.words(q) - STOP
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
            picks.append({"capability": m["capability"], "score": m["score"], "op": o.name, "summary": o.summary, "risk": o.risk,
                          "selected_tool": first["tool"] if first else None, "implementations": usable, "params": o.params,
                          **({"upgrade": upgrade} if upgrade else {})})
    if not picks:
        caps = match_capabilities(intent, 3)
        return {"intent": intent, "selection": None, "note": "no executable op; see `discover` for catalogue tools", "capabilities": caps}
    # on equal relevance prefer what is runnable here, then the op that changes nothing
    best = max(picks, key=lambda p: (p["selected_tool"] is not None, p["score"] - (0 if p["risk"] == O.READ_ONLY else 1)))
    req = {k: f"<{v.split(' ', 1)[1] if ' ' in v else 'value'}>" for k, v in best["params"].items() if str(v).startswith("REQUIRED")}
    tool = "rth_run" if best["risk"] == O.READ_ONLY else "rth_run_approved"
    nxt = {"tool": tool, "arguments": {"op": best["op"], "params": {**req, "...": "optional params from 'params'"}}}
    return {"intent": intent, "selection": best, "next": nxt, "alternatives": [p for p in picks if p is not best][:4]}
