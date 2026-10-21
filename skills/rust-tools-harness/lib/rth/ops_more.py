"""More catalogue tools as executable ops (grex, fend/eva, lemmeknow, pastel, task runners, qsv/xsv, zola,
sccache, jj, himalaya), each with an honest fallback where one exists."""
import ast
import collections
import csv
import json
import operator
import os
import re
import shlex
from pathlib import Path

from . import ops as O
from .ops import Impl, Op, op, flag, lines, NETWORK, READ_ONLY, RUNS_CODE


# ----------------------------------------------------------------------------- regex-generate

def _regex_builtin(p, env):
    ex = [str(x) for x in p["examples"]]
    rx = "^(?:" + "|".join(re.escape(x) for x in sorted(set(ex), key=lambda s: (-len(s), s))) + ")$"
    return {"regex": rx, "matches_all": all(re.fullmatch(rx[1:-1], x) for x in ex)}


op(Op("regex-generate", "regex-generate", "Generate a regex matching example strings",
      {"examples": "REQUIRED list of strings", "digits": "bool: \\d classes", "repetitions": "bool: detect repeats", "words": "bool: \\w"},
      [Impl("grex", lambda p, env: ["grex", *flag(p.get("digits"), "-d"), *flag(p.get("repetitions"), "-r"), *flag(p.get("words"), "-w"),
                                    "--", *[str(x) for x in p["examples"]]],
            lambda o, p: {"regex": o.strip()}, registry_id="grex", semantics="grex: generalizes with character classes when asked."),
       Impl("builtin", run=_regex_builtin, semantics="Exact alternation of the examples (no generalization).")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- calculate

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _safe_eval(expr):
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            if isinstance(n.op, ast.Pow) and abs(ev(n.right)) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only numbers and + - * / // % ** ( ) are supported by the builtin calculator")
    return ev(ast.parse(expr.replace("^", "**"), mode="eval"))


op(Op("calculate", "calculator", "Evaluate arithmetic (fend also does units: '5 GiB to MB', '3 miles in km')",
      {"expr": "REQUIRED expression"},
      [Impl("fend", lambda p, env: ["fend", p["expr"]], lambda o, p: {"result": o.strip()}, registry_id="fend",
            semantics="fend: arbitrary precision, units, dates."),
       Impl("eva", lambda p, env: ["eva", p["expr"]], lambda o, p: {"result": o.strip()}, registry_id="eva"),
       Impl("builtin", run=lambda p, env: {"result": _safe_eval(p["expr"])}, semantics="Python AST arithmetic only (no units, no names).")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- identify-strings

IDENT = [("uuid", r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
         ("email", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), ("url", r"https?://[^\s]+"),
         ("ipv4", r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"),
         ("jwt", r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), ("sha256", r"[0-9a-f]{64}"), ("sha1", r"[0-9a-f]{40}"), ("md5", r"[0-9a-f]{32}"),
         ("mac-address", r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}"), ("aws-access-key-id", r"AKIA[0-9A-Z]{16}")]


def _identify_builtin(p, env):
    s = p["text"].strip()
    return {"matches": [{"name": n, "exact": True} for n, rx in IDENT if re.fullmatch(rx, s, re.I)] or
            [{"name": n, "exact": False, "found": m[0]} for n, rx in IDENT for m in [re.search(rx, s, re.I)] if m]}


op(Op("identify", "identify-strings", "Identify what a string is (hash, IP, email, JWT, UUID, key...)",
      {"text": "REQUIRED string"},
      [Impl("lemmeknow", lambda p, env: ["lemmeknow", "--json", p["text"]], json.loads, registry_id="lemmeknow",
            semantics="lemmeknow: ~300 patterns with rarity scores."),
       Impl("builtin", run=_identify_builtin, semantics="10 common patterns (uuid, email, url, ipv4, jwt, sha256/sha1/md5, mac, AWS key id).")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- color

def _color_builtin(p, env):
    c = p["color"].strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", c):
        c = "".join(ch * 2 for ch in c)
    m = re.fullmatch(r"[0-9a-fA-F]{6}", c) and c
    if not m:
        rgb = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", p["color"].strip(), re.I)
        if not rgb:
            raise ValueError("builtin understands #rgb, #rrggbb and rgb(r,g,b) only")
        r, g, b = (int(x) for x in rgb.groups())
    else:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    import colorsys
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return {"hex": f"#{r:02x}{g:02x}{b:02x}", "rgb": f"rgb({r}, {g}, {b})", "hsl": f"hsl({h * 360:.0f}, {s * 100:.0f}%, {l * 100:.0f}%)"}


op(Op("color", "color-tool", "Convert a color between hex / rgb / hsl",
      {"color": "REQUIRED e.g. '#ff8800', 'rgb(1,2,3)', 'steelblue' (names need pastel)"},
      [Impl("pastel", lambda p, env: ["sh", "-c", 'for f in hex rgb hsl; do printf "%s=" $f; pastel format $f "$1"; done', "pastel", p["color"]],
            lambda o, p: dict(ln.split("=", 1) for ln in lines(o) if "=" in ln), registry_id="pastel", requires=("sh",)),
       Impl("builtin", run=_color_builtin, semantics="hex/rgb parsing only; no color names.")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- task-run

def _task_argv(p, env):
    runner, name = p.get("runner"), p["task"]
    root = p.get("path", ".")
    if not runner:
        found = O._task_builtin({"path": root}, env)
        runner = next((r for r in ("just", "mask", "make", "npm", "mise", "cargo") if isinstance(found.get(r), list) and
                       (name in found[r] or r == "cargo")), None)
        if not runner:
            return None
    args = [str(a) for a in (p.get("args") or [])]
    return {"just": ["just", "--justfile", _justfile(root), "--working-directory", root, name, *args],
            "mask": ["mask", "--maskfile", os.path.join(root, "maskfile.md"), name, *args],
            "make": ["make", "-C", root, name, *args],
            "npm": ["npm", "--prefix", root, "run", name, "--", *args],
            "mise": ["mise", "-C", root, "run", name, *args],
            "cargo": ["cargo", *shlex.split(name), "--manifest-path", os.path.join(root, "Cargo.toml"), *args]}[runner]


def _justfile(root):
    return next((os.path.join(root, f) for f in ("justfile", "Justfile", ".justfile") if os.path.exists(os.path.join(root, f))), "justfile")


def _task_impl(runner):
    return Impl(runner, lambda p, env, r=runner: _task_argv({**p, "runner": p.get("runner") or r}, env)
                if (p.get("runner") in (None, r)) else None,
                lambda o, p: {"output_tail": o[-4000:]}, registry_id={"just": "just", "mask": "mask", "mise": "mise"}.get(runner),
                when=lambda p, env, r=runner: None if (p.get("runner") == r or (not p.get("runner") and _task_argv(p, env)
                                                      and _task_argv(p, env)[0] == r)) else f"task not defined for {r}")


op(Op("task-run", "task-runner", "Run a project task found by task-list (just/mask/make/npm/mise/cargo); exit code = task result",
      {"task": "REQUIRED recipe/target/script name (for cargo: 'test', 'build --release')", "runner": "force a runner",
       "path": ".", "args": "extra args (list)"},
      [_task_impl(r) for r in ("just", "mask", "make", "npm", "mise", "cargo")],
      risk=RUNS_CODE, timeout=1800, verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- csv frequency / select

def _csv_builtin_freq(p, env):
    cnt = collections.Counter()
    with open(p["file"], newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if p["column"] not in row:
                raise ValueError(f"no column {p['column']!r}")
            cnt[row[p["column"]]] += 1
    return [{"field": p["column"], "value": v, "count": c} for v, c in cnt.most_common(int(p.get("limit", 20)))]


op(Op("csv-frequency", "csv-query", "Most frequent values of a CSV column",
      {"file": "REQUIRED", "column": "REQUIRED", "limit": "top N (20)"},
      [Impl("qsv", lambda p, env: ["qsv", "frequency", "-s", p["column"], "-l", str(p.get("limit", 20)), p["file"]], O._csv_rows, registry_id="qsv"),
       Impl("xsv", lambda p, env: ["xsv", "frequency", "-s", p["column"], "-l", str(p.get("limit", 20)), p["file"]], O._csv_rows, registry_id="xsv"),
       Impl("builtin", run=_csv_builtin_freq, semantics="Python Counter; exact.")],
      verify={"min_records": 1}))


def _csv_builtin_select(p, env):
    cols = [c.strip() for c in (p["columns"] if isinstance(p["columns"], list) else str(p["columns"]).split(","))]
    out = []
    with open(p["file"], newline="", encoding="utf-8", errors="replace") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if i >= int(p.get("limit", 1000)):
                break
            out.append({c: row.get(c) for c in cols})
    return out


op(Op("csv-select", "csv-transform", "Project CSV columns (and optionally filter rows by a column regex)",
      {"file": "REQUIRED", "columns": "REQUIRED 'a,b' or list", "filter_column": "column to match", "filter": "regex", "limit": "rows (1000)"},
      [Impl("qsv", lambda p, env: ["sh", "-c", 'f="$1"; shift; if [ -n "$2" ]; then qsv search -s "$2" "$3" "$f" | qsv select "$1"; else qsv select "$1" "$f"; fi',
                                   "x", p["file"], ",".join(p["columns"]) if isinstance(p["columns"], list) else p["columns"],
                                   p.get("filter_column", ""), p.get("filter", "")],
            O._csv_rows, registry_id="qsv", requires=("sh",)),
       Impl("builtin", run=lambda p, env: [r for r in _csv_builtin_select(p, env)] if not p.get("filter") else
            [r for r in _csv_builtin_select({**p, "columns": (p["columns"] if isinstance(p["columns"], list) else p["columns"].split(",")) + [p["filter_column"]]}, env)
             if re.search(p["filter"], r.get(p["filter_column"]) or "")],
            semantics="Python csv; filter with Python re.")],
      verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- zola / sccache / jj / himalaya

op(Op("site-check", "static-site", "Validate a zola site (content, internal links; external links unless skipped)",
      {"path": "site root (.)", "skip_external": "bool (default true: no network)"},
      [Impl("zola", lambda p, env: ["zola", "--root", p.get("path", "."), "check",
                                    *flag(p.get("skip_external", True) and env.supports_flag("zola", "--skip-external-links"), "--skip-external-links")],
            lines, registry_id="zola", semantics="zola check: with external links it makes HTTP requests.")],
      verify={"exit_in": [0]}))

op(Op("cache-stats", "compile-cache", "sccache hit/miss statistics",
      {},
      [Impl("sccache", lambda p, env: ["sccache", "--show-stats", *flag(env.supports_flag("sccache", "--stats-format"), "--stats-format", "json")],
            lambda o, p: json.loads(o) if o.strip().startswith("{") else lines(o), registry_id="sccache",
            semantics="Starts the sccache server if it is not running.")],
      verify={"not_empty": True}))

op(Op("jj-log", "vcs", "Jujutsu history and working-copy status without snapshotting (read-only)",
      {"path": "repo (.)", "n": "entries (20)"},
      [Impl("jj", lambda p, env: ["jj", "-R", p.get("path", "."), "--ignore-working-copy", "--no-pager", "--color", "never",
                                  "log", "-n", str(p.get("n", 20))], lines, registry_id="jj",
            semantics="--ignore-working-copy: does not snapshot, so very recent edits may not show.")],
      verify={"exit_in": [0]}))

op(Op("mail-list", "email", "List mailbox envelopes via himalaya (user's mail: only on explicit request)",
      {"folder": "INBOX", "limit": "20", "account": "himalaya account name"},
      [Impl("himalaya", lambda p, env: ["himalaya", "envelope", "list", *flag(p.get("account"), "-a", p.get("account") or ""),
                                        "-f", p.get("folder", "INBOX"), "-s", str(p.get("limit", 20)), "-o", "json"],
            json.loads, registry_id="himalaya", semantics="Reads private mail over the network.")],
      risk=NETWORK, verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- benchmark parameter scan

_bench = O.OPS["benchmark"]
_bench.params["parameter_scan"] = "hyperfine -P: 'VAR MIN MAX' (use {VAR} in commands)"
_orig_build = _bench.impls[0].build


def _hf_with_scan(p, env):
    a = _orig_build(p, env)
    if p.get("parameter_scan"):
        var, lo, hi = str(p["parameter_scan"]).split()
        a[1:1] = ["-P", var, lo, hi]
    return a


_bench.impls[0].build = _hf_with_scan
for _i in _bench.impls:
    if _i.tool == "builtin":
        _i.when = lambda p, env: "parameter scans need hyperfine" if p.get("parameter_scan") else None
