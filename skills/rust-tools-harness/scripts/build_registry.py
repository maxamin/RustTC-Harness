#!/usr/bin/env python3
"""Build the rust-tools-harness registry.

Pipeline (each stage is re-runnable on its own):

  parse   README.md                      -> registry/catalogue.json
  fetch   catalogue + upstream sources   -> registry/upstream.json   (network)
  build   catalogue + upstream + curated -> registry/tools.json
  docs    tools.json                     -> references/catalogue.md (also run by build)

Upstream sources used by `fetch` (no GitHub token required):
  * https://github.com/<repo>             archived banner, redirects, languages
  * https://github.com/<repo>/commits.atom   date of the latest commit
  * https://github.com/<repo>/releases/latest  latest release tag (redirect)
  * raw.githubusercontent.com/<repo>/HEAD/Cargo.toml  package + [[bin]] names
  * https://crates.io/api/v1/crates/<name>   published crate, binary names,
    accepted only when the crate's `repository` points at the same repo.

Anything not confirmed by one of these sources stays marked as curated or
unknown in tools.json; the build never invents a verified value.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
REG = SKILL / "registry"
UA = "rust-tools-harness-registry-builder (+https://github.com/unpluggedcoder/awesome-rust-tools)"

ENTRY_RE = re.compile(r"^- \[(?P<name>[^\]]+)\]\((?P<url>[^)]+)\)\s*[—–-]?\s*(?P<desc>.*)$")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_readme(readme):
    entries, category = [], None
    for line in Path(readme).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            category = line[3:].strip()
            continue
        m = ENTRY_RE.match(line.strip())
        if not m or category in (None, "Table of contents"):
            continue
        name = m["name"].strip()
        archived_hint = "(archived)" in name.lower()
        name = re.sub(r"\(archived\)", "", name, flags=re.I).strip()
        url = m["url"].strip()
        repo = None
        gm = re.match(r"https?://github\.com/([^/]+/[^/#?]+)", url)
        if gm:
            repo = gm[1].removesuffix(".git")
        entries.append({
            # same display name listed twice (e.g. two "forge"s): suffix the owner
            "id": slug(name) if slug(name) not in {e["id"] for e in entries} else slug(name) + "-" + slug((repo or url).split("/")[0]),
            "name": name,
            "url": url,
            "repo": repo,
            "category": category,
            "description": m["desc"].strip(),
            "readme_marks_archived": archived_hint,
        })
    return entries


# --------------------------------------------------------------------------- fetch

_crates_lock = threading.Lock()
_crates_last = [0.0]


def http_get(url, *, accept=None, crates=False, timeout=30):
    if crates:  # crates.io crawler policy: max 1 request/second
        with _crates_lock:
            wait = 1.05 - (time.monotonic() - _crates_last[0])
            if wait > 0:
                time.sleep(wait)
            _crates_last[0] = time.monotonic()
    headers = {"User-Agent": UA, **({"Accept": accept} if accept else {})}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":  # token goes to the GitHub API only
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, url, ""
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < 2:
                time.sleep(2)
                continue
            return None, url, ""
    return None, url, ""


def norm_repo(url):
    if not url:
        return None
    m = re.search(r"github\.com[/:]([^/]+/[^/#?]+)", url)
    return m[1].removesuffix(".git").lower() if m else None


def fetch_github(repo):
    out = {"repo": repo}
    status, final, html = http_get(f"https://github.com/{repo}")
    out["http_status"] = status
    if status != 200:
        return out
    final_repo = norm_repo(final)
    out["canonical_repo"] = re.search(r"github\.com/([^/]+/[^/#?]+)", final)[1]
    out["redirected"] = final_repo != repo.lower()
    m = re.search(r"This repository was archived by the owner on ([^.<]+)", html)
    out["archived"] = bool(m)
    out["archived_on"] = m[1].strip() if m else None
    m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    out["og_description"] = m[1] if m else None
    canon = out["canonical_repo"]
    s, _, atom = http_get(f"https://github.com/{canon}/commits.atom")
    m = re.search(r"<entry>.*?<updated>([^<]+)</updated>", atom, re.S) if s == 200 else None
    out["last_commit"] = m[1] if m else None
    s, final, _ = http_get(f"https://github.com/{canon}/releases/latest")
    m = re.search(r"/releases/tag/([^/?#]+)$", final) if s == 200 else None
    out["latest_release"] = urllib.parse.unquote(m[1]) if m else None
    out["cargo"] = fetch_cargo(canon)
    return out


def fetch_cargo(repo):
    s, _, text = http_get(f"https://raw.githubusercontent.com/{repo}/HEAD/Cargo.toml")
    if s != 200:
        return {"found": False}
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {"found": True, "parse_error": str(e)}
    info = {"found": True, "packages": []}

    def add(d, where):
        pkg = d.get("package") or {}
        if pkg.get("name"):
            info["packages"].append({
                "name": pkg["name"], "path": where,
                "bins": [b.get("name") for b in d.get("bin", []) if b.get("name")],
                "has_lib": "lib" in d,
            })

    add(doc, ".")
    members = (doc.get("workspace") or {}).get("members", [])
    info["workspace_members"] = members
    for mem in members[:12]:
        if "*" in mem:
            continue
        s, _, t = http_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{mem}/Cargo.toml")
        if s == 200:
            try:
                add(tomllib.loads(t), mem)
            except tomllib.TOMLDecodeError:
                pass
    return info


def fetch_crate(repo_variants, candidates):
    tried = []
    for name in candidates:
        if name in tried:
            continue
        tried.append(name)
        s, _, body = http_get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}", crates=True)
        if s != 200:
            continue
        d = json.loads(body)
        c = d["crate"]
        if norm_repo(c.get("repository")) not in repo_variants and norm_repo(c.get("homepage")) not in repo_variants:
            continue
        live = [v for v in d.get("versions", []) if not v.get("yanked")]
        latest = next((v for v in live if v["num"] == c.get("max_stable_version")), live[0] if live else {})
        return {
            "crate": c["name"],
            "max_stable_version": c.get("max_stable_version"),
            "updated_at": c.get("updated_at"),
            "bin_names": latest.get("bin_names") or [],
            "rust_version": latest.get("rust_version"),
            "license": latest.get("license"),
            "tried": tried,
        }
    return {"crate": None, "tried": tried}


def fetch_one(entry, hints, prev=None):
    rec = {"id": entry["id"], "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if not entry["repo"]:
        rec["note"] = "not a GitHub URL; not fetched"
        return rec
    gh = prev["github"] if prev and prev.get("github") else fetch_github(entry["repo"])
    rec["github"] = gh
    variants = {entry["repo"].lower()}
    if gh.get("canonical_repo"):
        variants.add(gh["canonical_repo"].lower())
    cands = list(hints.get(entry["id"], []))
    for p in (gh.get("cargo") or {}).get("packages", []):
        cands.append(p["name"])
    cands += [entry["repo"].split("/")[1].lower(), entry["id"]]
    rec["crates_io"] = fetch_crate(variants, cands)
    return rec


def tree_bins(repo, want=None):
    """All binary targets in a repo: one GitHub tree API call + raw Cargo.toml reads (no API cost)."""
    s, _, body = http_get(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", accept="application/vnd.github+json")
    if s != 200:
        return {"error": f"tree API http {s}"}
    paths = [t["path"] for t in json.loads(body).get("tree", []) if t.get("type") == "blob"]
    pset = set(paths)
    tomls = [p for p in paths if p.endswith("Cargo.toml") and "/tests/" not in p and "/examples/" not in p]
    if want:  # big workspaces: look at the crate directories named like the binary first
        tomls.sort(key=lambda p: (want not in p.split("/")[-2:-1] and p != "Cargo.toml", want not in p, len(p)))
    tomls = tomls[:60]
    bins = {}
    for t in tomls:
        st, _, text = http_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{t}")
        if st != 200:
            continue
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue
        pkg = (doc.get("package") or {}).get("name")
        base = t[: -len("Cargo.toml")]
        for b in doc.get("bin", []):
            if b.get("name"):
                bins[b["name"]] = t
        if pkg and (base + "src/main.rs") in pset and (doc.get("package") or {}).get("autobins", True) is not False:
            bins.setdefault(pkg, t)
        for p in paths:  # src/bin/<name>.rs and src/bin/<name>/main.rs
            m = re.fullmatch(re.escape(base) + r"src/bin/([^/]+?)(?:\.rs|/main\.rs)", p)
            if m and pkg:
                bins.setdefault(m[1], t)
    return {"bins": bins, "cargo_tomls": len(tomls)}


def cmd_verify_bins(args):
    """Confirm curated binary names against real build targets (for tools crates.io/root Cargo.toml didn't confirm)."""
    reg = json.loads((REG / "tools.json").read_text())
    up_path = REG / "upstream.json"
    up = json.loads(up_path.read_text())
    todo = [t for t in reg["tools"] if (t["binary"] and t["verification"]["binary_source"] == "curated"
            or args.unknown and not t["binary"] and t["kind"] in ("cli", "tui", "shell", "agent", "service"))
            and (not args.only or t["id"] in args.only)]
    for t in todo:
        repo = t["url"].split("github.com/")[1]
        info = tree_bins(repo, want=t["binary"])
        up["tools"][t["id"]]["tree_bins"] = {**info, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        found = (t["binary"] or "") in info.get("bins", {})
        up_path.write_text(json.dumps(up, indent=1, ensure_ascii=False) + "\n")  # save per tool: API quota is precious
        print(f"{t['id']:<14} {str(t['binary']):<12} {'CONFIRMED' if found else 'scanned' if not t['binary'] else 'not found'}  {sorted(info.get('bins', {}))[:8] or info.get('error')}",
              file=sys.stderr)
    up_path.write_text(json.dumps(up, indent=1, ensure_ascii=False) + "\n")


def cmd_fetch(args):
    entries = json.loads((REG / "catalogue.json").read_text())
    curated = json.loads((REG / "curated.json").read_text()) if (REG / "curated.json").exists() else {}
    hints = {k: ([v["crate"]] if v.get("crate") else []) for k, v in curated.get("tools", {}).items()}
    out_path = REG / "upstream.json"
    prev = json.loads(out_path.read_text()) if out_path.exists() else {}
    results = dict(prev.get("tools", {}))
    if args.crates_only:  # reuse GitHub data, redo crates.io lookup where no crate matched
        todo = [e for e in entries if not (results.get(e["id"], {}).get("crates_io") or {}).get("crate")]
    else:
        todo = [e for e in entries if args.refresh or e["id"] not in results]
    if args.only:
        todo = [e for e in todo if e["id"] in args.only]
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        reuse = results if args.crates_only else {}
        futs = {ex.submit(fetch_one, e, hints, reuse.get(e["id"])): e for e in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            e = futs[f]
            try:
                results[e["id"]] = f.result()
            except Exception as exc:  # keep going; record the failure
                results[e["id"]] = {"id": e["id"], "error": repr(exc)}
            print(f"[{i}/{len(todo)}] {e['id']}", file=sys.stderr, flush=True)
            if i % 10 == 0:
                _save(out_path, results)
    _save(out_path, results)


def _save(path, results):
    path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tools": dict(sorted(results.items())),
    }, indent=1, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- build

def status_of(gh, entry):
    if not gh or gh.get("http_status") != 200:
        return "unverified"
    if gh.get("archived"):
        return "archived"
    lc = gh.get("last_commit")
    if lc:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(lc.replace("Z", "+00:00"))
        if age.days > 730:
            return "inactive"
    return "active"


def cmd_build(args):
    entries = json.loads((REG / "catalogue.json").read_text())
    up = json.loads((REG / "upstream.json").read_text()).get("tools", {}) if (REG / "upstream.json").exists() else {}
    cur = json.loads((REG / "curated.json").read_text())
    ctools, defaults = cur["tools"], cur.get("defaults", {})
    missing = [e["id"] for e in entries if e["id"] not in ctools]
    if missing:
        print("warning: no curated metadata for: " + ", ".join(missing), file=sys.stderr)
    tools = []
    for e in entries:
        c = {**defaults, **ctools.get(e["id"], {})}
        u = up.get(e["id"], {})
        gh, ci = u.get("github") or {}, u.get("crates_io") or {}
        cargo_bins = sorted({b for p in (gh.get("cargo") or {}).get("packages", []) for b in p["bins"]} |
                            set((u.get("tree_bins") or {}).get("bins", {})))
        verified_bins = ci.get("bin_names") or cargo_bins
        binary = c.get("binary")
        if binary is None and verified_bins and c.get("kind") in ("cli", "tui", "shell", "agent", "service"):
            names = {e["id"], e["name"].lower(), (e["repo"] or "").split("/")[-1].lower()}
            match = [b for b in verified_bins if b.lower() in names]
            binary = match[0] if match else (verified_bins[0] if len(verified_bins) == 1 else None)
        if binary and binary in verified_bins:
            bin_src = "crates.io" if binary in (ci.get("bin_names") or []) else "Cargo.toml"
        elif binary:
            bin_src = "curated"
        else:
            bin_src = "unknown"
        install = list(c.get("install", []))
        if ci.get("crate") and verified_bins and not any(i.startswith("cargo install") for i in install):
            install.insert(0, f"cargo install --locked {ci['crate']}")
        status = c.get("status_override") or status_of(gh, e)
        if c.get("experimental") and status == "active":
            status = "experimental"
        tool = {
            "id": e["id"],
            "name": e["name"],
            "url": f"https://github.com/{gh['canonical_repo']}" if gh.get("canonical_repo") else e["url"],
            "listed_url": e["url"],
            "category": e["category"],
            "purpose": e["description"],
            "kind": c.get("kind", "unknown"),
            "binary": binary,
            "aliases": c.get("aliases", []),
            "capabilities": c.get("capabilities", []),
            "platforms": c.get("platforms", "unknown"),
            "dependencies": c.get("dependencies", []),
            "install": install,
            "input": c.get("input", "unknown"),
            "output": c.get("output", "unknown"),
            "structured_output": c.get("structured_output", []),
            "useful_flags": c.get("flags", {}),
            "exit_behavior": c.get("exit", "unknown"),
            "interactive": c.get("interactive", "unknown"),
            "automation": c.get("automation", "unknown"),
            "risk": c.get("risk", "read-only"),
            "limitations": c.get("limitations", []),
            "alternatives": c.get("alternatives", []),
            "related": c.get("related", []),
            "workflows": c.get("workflows", []),
            "status": status,
            "verification": {
                "upstream_checked": u.get("fetched_at"),
                "repo_reachable": gh.get("http_status") == 200,
                "redirected_to": gh.get("canonical_repo") if gh.get("redirected") else None,
                "archived_on": gh.get("archived_on"),
                "last_commit": gh.get("last_commit"),
                "latest_release": gh.get("latest_release"),
                "has_cargo_toml": (gh.get("cargo") or {}).get("found"),
                "crate": ci.get("crate"),
                "crate_version": ci.get("max_stable_version"),
                "crate_bins": ci.get("bin_names", []),
                "cargo_toml_bins": cargo_bins,
                "binary_source": bin_src,
                "metadata_source": "curated" if e["id"] in ctools else "none",
            },
            "notes": c.get("notes"),
        }
        tools.append(tool)
    reg = {
        "schema": "rust-tools-harness/registry@1",
        "source": "https://github.com/unpluggedcoder/awesome-rust-tools",
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(tools),
        "tools": tools,
    }
    (REG / "tools.json").write_text(json.dumps(reg, indent=1, ensure_ascii=False) + "\n")
    by = {}
    for t in tools:
        by[t["status"]] = by.get(t["status"], 0) + 1
    print(f"wrote {len(tools)} tools to registry/tools.json; status: {by}", file=sys.stderr)
    cmd_docs(args)


def cmd_docs(args):
    """registry/tools.json -> references/catalogue.md (human-readable, generated)."""
    reg = json.loads((REG / "tools.json").read_text())
    out = ["# Catalogue (generated)", "",
           f"Generated from `registry/tools.json` ({reg['count']} tools, built {reg['built_at']}). Do not edit by hand: "
           "run `scripts/build_registry.py build`.", "",
           "Columns: **bin** = binary (`?` unknown; `*` curated, not upstream-verified) · **auto** = automation suitability · "
           "**ia** = interactive (none / optional / required).", ""]
    cats = []
    for t in reg["tools"]:
        if t["category"] not in cats:
            cats.append(t["category"])
    for c in cats:
        out += [f"## {c}", "", "| tool | bin | kind | status | auto | ia | capabilities |", "|---|---|---|---|---|---|---|"]
        for t in (x for x in reg["tools"] if x["category"] == c):
            b = t["binary"] or "?"
            if t["binary"] and t["verification"]["binary_source"] == "curated":
                b += "*"
            out.append(f"| [{t['name']}]({t['url']}) | `{b}` | {t['kind']} | {t['status']} | {t['automation']} | "
                       f"{t['interactive']} | {', '.join(t['capabilities'])} |")
        out.append("")
    (SKILL / "references" / "catalogue.md").write_text("\n".join(out))
    print("wrote references/catalogue.md", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("readme", nargs="?", default=str(SKILL.parent.parent / "README.md"))
    f = sub.add_parser("fetch")
    f.add_argument("--refresh", action="store_true", help="re-fetch tools already in upstream.json")
    f.add_argument("--only", nargs="*", help="fetch only these tool ids")
    f.add_argument("--crates-only", action="store_true", help="redo crates.io matching only, reusing GitHub data")
    f.add_argument("--workers", type=int, default=6)
    sub.add_parser("build")
    vb = sub.add_parser("verify-bins", help="confirm curated binary names via the repo's Cargo.toml build targets (GitHub tree API)")
    vb.add_argument("--only", nargs="*")
    vb.add_argument("--unknown", action="store_true", help="also scan CLI/agent/service tools whose binary is unknown")
    sub.add_parser("docs")
    a = ap.parse_args()
    if a.cmd == "parse":
        entries = parse_readme(a.readme)
        (REG / "catalogue.json").write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
        print(f"parsed {len(entries)} entries", file=sys.stderr)
    elif a.cmd == "fetch":
        cmd_fetch(a)
    elif a.cmd == "verify-bins":
        cmd_verify_bins(a)
    elif a.cmd == "docs":
        cmd_docs(a)
    else:
        cmd_build(a)


if __name__ == "__main__":
    main()
