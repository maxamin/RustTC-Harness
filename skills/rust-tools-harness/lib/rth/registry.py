"""Registry access: tools.json (catalogue) + capabilities.json (taxonomy)."""
import json
import re
from functools import lru_cache
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
REGISTRY_DIR = SKILL_DIR / "registry"
WORKFLOW_DIR = SKILL_DIR / "workflows"

AUTOMATION_SCORE = {"high": 3, "medium": 2, "low": 1, "none": 0, "unknown": 1}
STATUS_PENALTY = {"active": 0, "experimental": -1, "inactive": -1, "archived": -2, "unverified": -3}


@lru_cache(maxsize=1)
def tools():
    data = json.loads((REGISTRY_DIR / "tools.json").read_text(encoding="utf-8"))
    return {t["id"]: t for t in data["tools"]}


@lru_cache(maxsize=1)
def capabilities():
    return json.loads((REGISTRY_DIR / "capabilities.json").read_text(encoding="utf-8"))["capabilities"]


def get(tool_id_or_binary):
    """Look a tool up by id, display name, binary or alias."""
    key = tool_id_or_binary.lower()
    t = tools()
    if key in t:
        return t[key]
    for tool in t.values():
        if key in (tool["name"].lower(), (tool["binary"] or "").lower()) or key in [a.lower() for a in tool["aliases"]]:
            return tool
    return None


def tools_for_capability(cap):
    return [t for t in tools().values() if cap in t["capabilities"]]


def binaries():
    """binary -> [tool ids]; several tools can claim the same name (hx, tldr, trash)."""
    out = {}
    for t in tools().values():
        for b in filter(None, [t["binary"], *t["aliases"]]):
            out.setdefault(b, []).append(t["id"])
    return out


def identity_tokens(tool):
    """Strings expected in `<bin> --version` output to confirm the binary is this tool."""
    toks = {tool["id"], tool["name"].lower(), tool["verification"].get("crate") or ""}
    from .env import NATIVE_BINARIES  # a system `sudo`/`tldr` must not confirm sudo-rs/tealdeer
    shared = binaries()
    for b in filter(None, [tool["binary"], *tool["aliases"]]):
        if len(shared.get(b, [])) == 1 and b not in NATIVE_BINARIES:
            toks.add(b.lower())
    toks.discard("")
    return {t for t in toks if len(t) > 1}


ROOT_PMS = {"apt", "dnf", "yum", "pacman", "zypper", "apk"}


def install_hints(tool, env, limit=3):
    """Ways to install `tool` that work on THIS machine (available package managers), best first.
    Never executed by the harness; installing is the user's decision."""
    pms = set(env.data.get("package_managers", []))
    is_root = env.data.get("privileges", {}).get("is_root")
    out = []
    crate = tool["verification"].get("crate")
    if crate and tool["verification"].get("crate_bins"):
        if "cargo-binstall" in pms:
            out.append({"command": f"cargo binstall --no-confirm --locked {crate}", "needs_root": False, "via": "cargo-binstall (prebuilt)"})
        if "cargo" in pms:
            out.append({"command": f"cargo install --locked {crate}", "needs_root": False, "via": "cargo (builds from source)"})
    for line in tool.get("install", []):
        pm = line.split()[0]
        if pm.startswith("cargo"):
            continue
        if pm in pms:
            root = pm in ROOT_PMS and not is_root
            out.append({"command": ("sudo " if root else "") + line.split(" (")[0], "needs_root": root, "via": pm,
                        **({"note": line[line.index("(") + 1:].rstrip(")")} if "(" in line else {})})
    if tool["verification"].get("latest_release"):
        out.append({"command": f"download a prebuilt binary: {tool['url']}/releases/latest", "needs_root": False, "via": "github release"})
    out.sort(key=lambda h: h["needs_root"])
    return out[:limit]


_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")


def words(text):
    return set(_WORD.findall((text or "").lower()))


def search(query, *, category=None, installed=None, kind=None, limit=20):
    """Rank registry tools for a free-text query. `installed` is a set of available ids (optional filter)."""
    q = query.lower().strip()
    qw = words(q)
    caps = capabilities()
    hits = []
    for t in tools().values():
        if category and t["category"].lower() != category.lower():
            continue
        if kind and t["kind"] != kind:
            continue
        if installed is not None and t["id"] not in installed:
            continue
        score, why = 0.0, []
        if q in (t["id"], t["name"].lower(), (t["binary"] or "").lower()):
            score += 10
            why.append("name")
        for c in t["capabilities"]:
            cw = words(c.replace("-", " "))
            kws = caps.get(c, {}).get("keywords", [])
            if any(k in q for k in kws if " " in k) or q in kws:
                score += 4
                why.append(f"cap:{c}")
            elif qw & (cw | words(" ".join(kws))):
                score += 2 * len(qw & (cw | words(" ".join(kws)))) / max(len(qw), 1)
                why.append(f"cap~{c}")
        overlap = qw & words(t["purpose"] + " " + t["category"])
        if overlap:
            score += 1.5 * len(overlap) / max(len(qw), 1)
            why.append("purpose")
        if score <= 0:
            continue
        score += STATUS_PENALTY.get(t["status"], 0) * 0.3
        hits.append((round(score, 2), t, sorted(set(why))))
    hits.sort(key=lambda h: (-h[0], h[1]["id"]))
    return hits[:limit]
