"""Operation adapters: capability-level operations with ranked implementations.

An Op is what an agent asks for ("search-content", "disk-usage"); each Impl is
one concrete way to do it with one tool. Impls are listed in preference order
and carry:
  build(p, env)  -> argv list (or None when the impl cannot express the params)
  parse(out, p)  -> normalized records (list[dict]) or dict; raise ValueError on
                    malformed output so the executor can fall back
  when(p, env)   -> None if usable, else a reason string
  semantics      -> how results differ from the op's reference behaviour;
                    surfaced in every result that used this impl
Builtin impls (tool="builtin") run in-process Python and exist only where no
external tool gives structured output or none is installed.
"""
import csv
import fnmatch
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import sandbox

READ_ONLY = "read-only"
WRITES = "writes-files"
NETWORK = "network-active"
PRIVILEGED = "needs-privilege"
RUNS_CODE = "runs-user-code"


@dataclass
class Impl:
    tool: str                       # binary to execute, or "builtin"
    build: Callable = None
    parse: Optional[Callable] = None
    when: Optional[Callable] = None
    registry_id: Optional[str] = None
    requires: tuple = ()
    ok_exit: tuple = (0,)
    semantics: str = ""
    stdin: Optional[Callable] = None  # returns text fed to stdin
    run: Optional[Callable] = None    # builtin implementation: (p, env) -> data
    risk: Optional[str] = None        # overrides op risk for this impl


@dataclass
class Op:
    name: str
    capability: str
    summary: str
    params: dict
    impls: list
    risk: str = READ_ONLY
    verify: dict = field(default_factory=dict)
    timeout: int = 120
    undo: Optional[Callable] = None   # write ops: p -> {"modify": [...], "create": [...]} for rollback


OPS = {}


def op(o):
    OPS[o.name] = o
    return o


def flag(cond, *args):
    return list(args) if cond else []


def lines(out, _p=None):
    return [ln for ln in out.splitlines() if ln.strip()]


def _limit(rows, p):
    n = p.get("limit")
    return rows[: int(n)] if n else rows


def _git_repo(path, env):
    if not env.has("git") or not os.path.isdir(path):
        return False
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return r.stdout.strip() == "true"


# ----------------------------------------------------------------------------- find-files

def _fd(p, env):
    a = ["--color", "never", "--glob", p.get("pattern") or "*"]
    a += flag(p.get("type"), "-t", p.get("type") or "")
    a += flag(p.get("ext"), "-e", p.get("ext") or "")
    a += flag(p.get("hidden"), "-H")
    a += flag(p.get("no_ignore"), "-I")
    a += flag(p.get("max_depth"), "-d", str(p.get("max_depth")))
    a += flag(p.get("min_size"), "-S", "+" + str(p.get("min_size")).lstrip("+"))
    a += flag(p.get("changed_within"), "--changed-within", str(p.get("changed_within")))
    return ["fd", *a, p.get("path", ".")]


def _find(p, env):
    a = ["find", p.get("path", ".")]
    a += flag(p.get("max_depth"), "-maxdepth", str(p.get("max_depth")))
    if not p.get("hidden"):
        a += ["-not", "-path", "*/.*"]
    else:
        a += ["-not", "-path", "*/.git/*"]
    a += flag(p.get("type"), "-type", p.get("type") or "")
    if p.get("ext"):
        a += ["-name", f"*.{p['ext']}"]
    if p.get("pattern") and p["pattern"] != "*":
        a += ["-name", p["pattern"]]
    if p.get("min_size"):
        m = re.fullmatch(r"\+?(\d+)([kKmMgG]?)", str(p["min_size"]))
        if not m:
            return None
        a += ["-size", f"+{m[1]}{m[2].lower() if m[2].lower() == 'k' else m[2].upper()}"]
    if p.get("changed_within"):
        m = re.fullmatch(r"(\d+)d", str(p["changed_within"]))
        if not m:
            return None
        a += ["-mtime", f"-{m[1]}"]
    return a


def _git_ls(p, env):
    if p.get("type") == "d" or p.get("min_size") or p.get("changed_within"):
        return None
    return ["git", "-C", p.get("path", "."), "ls-files", "--cached", "--others", "--exclude-standard"]


def _git_ls_parse(out, p):
    pat = p.get("pattern") or "*"
    rows = []
    for ln in lines(out):
        name = ln.rsplit("/", 1)[-1]
        if not p.get("hidden") and any(part.startswith(".") for part in ln.split("/")):
            continue
        if p.get("ext") and not name.endswith("." + p["ext"]):
            continue
        if p.get("max_depth") and ln.count("/") + 1 > int(p["max_depth"]):
            continue
        if fnmatch.fnmatch(name, pat):
            rows.append(os.path.join(p.get("path", "."), ln) if p.get("path", ".") != "." else ln)
    return rows


def _git_when(p, env):
    path = p.get("path", ".")
    if p.get("no_ignore"):
        return "no_ignore requested; git only sees non-ignored files"
    if getattr(env, "remote", False):
        code, out = env._run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"])
        return None if code == 0 and out.strip() == "true" else "not inside a git work tree (remote)"
    if not os.path.isdir(path):
        return "path is not a directory"
    return None if _git_repo(path, env) else "not inside a git work tree"


def _paths(out, p):
    return _limit([{"path": ln} for ln in lines(out)], p)


op(Op("find-files", "find-files", "Find files by glob/extension/type/size/mtime",
      {"pattern": "glob on file name (default *)", "path": ".", "type": "f|d|l", "ext": "extension without dot",
       "hidden": "bool", "no_ignore": "bool: include .gitignored files", "max_depth": "int",
       "min_size": "e.g. 10M (fd: SI units; find: MiB)", "changed_within": "e.g. 2d", "limit": "max records"},
      [Impl("fd", _fd, _paths, registry_id="fd",
            semantics="Respects .gitignore unless no_ignore; skips hidden unless hidden."),
       Impl("git", _git_ls, lambda o, p: _limit([{"path": x} for x in _git_ls_parse(o, p)], p),
            when=lambda p, env: _git_when(p, env),
            semantics="git ls-files: tracked + untracked-not-ignored files only; no directories."),
       Impl("find", _find, _paths,
            semantics="find does NOT honor .gitignore: ignored files (build output, node_modules) are included.")],
      verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- search-content

def _rg(p, env):
    a = ["rg", "--json"]
    a += flag(p.get("fixed"), "-F")
    a += flag(p.get("ignore_case"), "-i")
    a += flag(p.get("word"), "-w")
    a += flag(p.get("hidden"), "--hidden")
    a += flag(p.get("no_ignore"), "--no-ignore")
    a += flag(p.get("glob"), "-g", p.get("glob") or "")
    a += flag(p.get("max_count"), "-m", str(p.get("max_count")))
    return a + ["-e", p["pattern"], "--", p.get("path", ".")]


def _rg_parse(out, p):
    rows, stats = [], {}
    for ln in out.splitlines():
        if not ln.strip():
            continue
        ev = json.loads(ln)  # ValueError on malformed output -> fallback
        if ev["type"] == "match":
            d = ev["data"]
            text = d["lines"].get("text", "")
            rows.append({"path": d["path"].get("text"), "line": d["line_number"], "text": text.rstrip("\n")[:400]})
        elif ev["type"] == "summary":
            stats = ev["data"]["stats"]
    return {"matches": _limit(rows, p), "match_count": len(rows),
            "files": sorted({r["path"] for r in rows}), "truncated": bool(p.get("limit")) and len(rows) > int(p["limit"]),
            "stats": {k: stats.get(k) for k in ("matched_lines", "searches_with_match", "searches")} if stats else None}


def _grepish_parse(out, p):
    rows = []
    for ln in out.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", ln)
        if m:
            rows.append({"path": m[1], "line": int(m[2]), "text": m[3][:400]})
    return {"matches": _limit(rows, p), "match_count": len(rows), "files": sorted({r["path"] for r in rows})}


def _git_grep(p, env):
    a = ["git", "-C", p.get("path", "."), "grep", "-n", "-I", "--no-color", "--untracked"]
    a += ["-F"] if p.get("fixed") else ["-E"]
    a += flag(p.get("ignore_case"), "-i")
    a += flag(p.get("word"), "-w")
    a += flag(p.get("max_count"), "-m", str(p.get("max_count")))
    a += ["-e", p["pattern"], "--"]
    a += flag(p.get("glob"), p.get("glob") or "")
    return a


def _grep(p, env):
    a = ["grep", "-rnI", "--exclude-dir=.git"]
    a += ["-F"] if p.get("fixed") else ["-E"]
    a += flag(p.get("ignore_case"), "-i")
    a += flag(p.get("word"), "-w")
    a += flag(p.get("glob"), f"--include={p.get('glob')}")
    a += flag(p.get("max_count"), "-m", str(p.get("max_count")))
    return a + ["-e", p["pattern"], p.get("path", ".")]


op(Op("search-content", "search-content", "Search file contents by regex (or literal)",
      {"pattern": "REQUIRED regex (Rust syntax for rg; ERE for grep)", "path": ".", "fixed": "bool literal",
       "ignore_case": "bool", "word": "bool", "glob": "file glob filter", "hidden": "bool", "no_ignore": "bool",
       "max_count": "per-file limit", "limit": "max match records (default 500)"},
      [Impl("rg", _rg, _rg_parse, registry_id="ripgrep", ok_exit=(0, 1),
            semantics="Rust regex; honors .gitignore; skips hidden and binary files."),
       Impl("git", _git_grep, _grepish_parse, ok_exit=(0, 1),
            when=lambda p, env: _git_when(p, env),
            semantics="git grep -E (POSIX ERE): paths are relative to the repo dir; glob is a git pathspec."),
       Impl("grep", _grep, _grepish_parse, ok_exit=(0, 1),
            semantics="grep -E (POSIX ERE); does NOT honor .gitignore.")],
      verify={"exit_in": [0, 1]}))


# ----------------------------------------------------------------------------- view-file / hex / list / tree

def _range(p):
    s, e = int(p.get("start") or 1), p.get("end")
    return s, (int(e) if e else s + int(p.get("lines") or 200) - 1)


op(Op("view-file", "view-file", "Print a file (or line range) with line numbers",
      {"path": "REQUIRED", "start": "first line (1)", "end": "last line", "lines": "count if no end (200)",
       "highlight": "bool: use bat with ANSI colors (for humans)"},
      [Impl("bat", lambda p, env: ["bat", "--paging=never", "--style=numbers", "--color=always", "-r", "%d:%d" % _range(p), p["path"]],
            when=lambda p, env: None if p.get("highlight") else "highlight not requested (plain numbered text is better for agents)",
            registry_id="bat", semantics="ANSI-highlighted; meant for human display."),
       Impl("awk", lambda p, env: ["awk", "-v", "s=%d" % _range(p)[0], "-v", "e=%d" % _range(p)[1],
                                   'NR>=s && NR<=e {printf "%6d  %s\\n", NR, $0} NR>e {exit}', p["path"]],
            semantics="Plain numbered text.")],
      verify={"exit_in": [0], "not_empty": True}))

op(Op("hex-view", "hex-view", "Hex dump the first N bytes",
      {"path": "REQUIRED", "length": "bytes (256)", "skip": "offset (0)"},
      [Impl("hexyl", lambda p, env: ["hexyl", "--color", "never", "-n", str(p.get("length", 256)), "-s", str(p.get("skip", 0)), p["path"]], registry_id="hexyl"),
       Impl("xxd", lambda p, env: ["xxd", "-l", str(p.get("length", 256)), "-s", str(p.get("skip", 0)), p["path"]]),
       Impl("od", lambda p, env: ["od", "-A", "x", "-t", "x1z" if env.is_gnu else "x1", "-N", str(p.get("length", 256)), "-j", str(p.get("skip", 0)), p["path"]],
            semantics="GNU od shows an ASCII column; BSD/macOS od shows hex only.")],
      verify={"exit_in": [0], "not_empty": True}))

op(Op("list-dir", "list-dir", "Long directory listing with metadata",
      {"path": ".", "all": "bool (default true)"},
      [Impl("eza", lambda p, env: ["eza", "-l", *flag(p.get("all", True), "-a"), "--time-style=long-iso", "--color=never",
                                   "--icons=never", "--group-directories-first", p.get("path", ".")], lines, registry_id="eza"),
       Impl("ls", lambda p, env: ["ls", "-l", *flag(p.get("all", True), "-a"), p.get("path", ".")], lines)],
      verify={"exit_in": [0]}))

op(Op("tree", "tree-view", "Directory tree to a depth",
      {"path": ".", "depth": "levels (2)"},
      [Impl("eza", lambda p, env: ["eza", "--tree", "-L", str(p.get("depth", 2)), "--git-ignore", "--color=never", "--icons=never", p.get("path", ".")],
            lines, registry_id="eza", semantics="Hides .gitignored entries."),
       Impl("tree", lambda p, env: ["tree", "-L", str(p.get("depth", 2)), "--noreport", p.get("path", ".")], lines,
            semantics="Shows ignored files."),
       Impl("find", lambda p, env: ["find", p.get("path", "."), "-maxdepth", str(p.get("depth", 2)), "-not", "-path", "*/.git*"],
            lambda o, p: sorted(lines(o)), semantics="Flat sorted path list, shows ignored files.")],
      verify={"exit_in": [0], "not_empty": True}))


# ----------------------------------------------------------------------------- disk usage

def _du(p, env):
    a = ["du", "-k", "-d", str(p.get("depth", 1))]
    if p.get("one_fs", True):
        a.append("-x")
    if p.get("apparent"):
        a += ["--apparent-size"] if env.is_gnu else ["-A"]
    return a + [p.get("path", ".")]


def _du_parse(out, p):
    rows = []
    for ln in lines(out):
        kb, _, path = ln.partition("\t")
        if not kb.strip().isdigit():
            raise ValueError(f"unexpected du line: {ln!r}")
        rows.append({"path": path, "bytes": int(kb) * 1024})
    rows.sort(key=lambda r: -r["bytes"])
    return _limit(rows, {"limit": p.get("top", 20)})


def _dust_parse(out, p):
    tree = json.loads(out)
    rows = []

    def size(v):
        if isinstance(v, (int, float)):
            return int(v)
        m = re.fullmatch(r"([\d.]+)\s*([KMGTP]?)i?B?", str(v).strip(), re.I)
        if not m:
            raise ValueError(f"unparsed dust size {v!r}")
        return int(float(m[1]) * 1024 ** "_KMGTP".index(m[2].upper() or "_"))

    def walk(n, depth):
        rows.append({"path": n.get("name"), "bytes": size(n.get("size")), "depth": depth})
        for c in n.get("children", []):
            walk(c, depth + 1)

    walk(tree, 0)
    rows.sort(key=lambda r: -r["bytes"])
    return _limit(rows, {"limit": p.get("top", 20)})


op(Op("disk-usage", "disk-usage", "Biggest directories under a path (sizes in bytes)",
      {"path": ".", "depth": "levels (1)", "top": "entries (20)", "apparent": "bool apparent size", "one_fs": "bool (true)"},
      [Impl("dust", lambda p, env: ["dust", "-j", *flag(env.supports_flag("dust", "--output-format"), "-o", "b"),
                                    "-d", str(p.get("depth", 1)), "-n", str(p.get("top", 20)),
                                    *flag(p.get("apparent"), "-s"), *flag(p.get("one_fs", True), "-x"), p.get("path", ".")],
            _dust_parse, registry_id="dust",
            when=lambda p, env: None if env.supports_flag("dust", "--output-json") else "this dust build has no JSON output",
            semantics="dust -j -o b: exact bytes (older builds without -o print rounded units, ~1% precision)."),
       Impl("du", _du, _du_parse, ok_exit=(0, 1),
            semantics="du -k: exact KiB; exit 1 with partial results when some dirs are unreadable.")],
      verify={"exit_in": [0, 1], "min_records": 1}))


def _diskus_parse(out, p):
    """diskus prints a bare byte count when stdout is not a TTY, and '1.04 MB (1,036,288 bytes)' on a TTY."""
    m = re.fullmatch(r"\s*(\d+)\s*", out) or re.search(r"\(([\d,]+) bytes\)", out)
    if not m:
        raise ValueError("no byte count in diskus output")
    return {"path": p.get("path", "."), "bytes": int(m[1].replace(",", ""))}


op(Op("dir-size", "dir-size", "Total size of a directory in bytes",
      {"path": ".", "apparent": "bool"},
      [Impl("diskus", lambda p, env: ["diskus", *flag(p.get("apparent"), "-b"), p.get("path", ".")], _diskus_parse, registry_id="diskus"),
       Impl("du", lambda p, env: ["du", "-sk", *flag(p.get("apparent"), *(["--apparent-size"] if env.is_gnu else ["-A"])), p.get("path", ".")],
            lambda o, p: {"path": p.get("path", "."), "bytes": int(o.split()[0]) * 1024}, ok_exit=(0, 1))],
      verify={"exit_in": [0, 1]}))


def _df_parse(out, p):
    rows = []
    for ln in lines(out)[1:]:
        f = ln.split()
        if len(f) >= 6 and f[1].isdigit():
            rows.append({"filesystem": f[0], "size_bytes": int(f[1]) * 1024, "used_bytes": int(f[2]) * 1024,
                         "avail_bytes": int(f[3]) * 1024, "use_pct": int(f[4].rstrip("%") or 0), "mount": " ".join(f[5:])})
    return sorted(rows, key=lambda r: -r["use_pct"])


op(Op("disk-free", "disk-usage", "Filesystem capacity and usage",
      {"path": "optional mount/path"},
      [Impl("df", lambda p, env: ["df", "-kP", *flag(p.get("path"), p.get("path") or "")], _df_parse)],
      verify={"exit_in": [0], "min_records": 1}))


# ----------------------------------------------------------------------------- processes / system

def _ps(p, env):
    sort = "pmem" if p.get("sort") == "mem" else "pcpu"
    cols = "pid,ppid,user,pcpu,pmem,rss,etime,comm"
    if env.os == "linux":
        return ["ps", "-eo", cols, f"--sort=-{sort}"]
    return ["ps", "-Ao", cols, "-m" if sort == "pmem" else "-r"]


def _ps_parse(out, p):
    rows = []
    for ln in lines(out)[1:]:
        f = ln.split(None, 7)
        if len(f) < 8:
            continue
        rows.append({"pid": int(f[0]), "ppid": int(f[1]), "user": f[2], "cpu_pct": float(f[3]), "mem_pct": float(f[4]),
                     "rss_kb": int(f[5]), "elapsed": f[6], "command": f[7]})
    if p.get("filter"):
        rows = [r for r in rows if p["filter"].lower() in r["command"].lower()]
    return _limit(rows, {"limit": p.get("limit", 15)})


op(Op("process-list", "process-list", "Top processes by CPU or memory",
      {"sort": "cpu|mem", "filter": "substring of command", "limit": "rows (15)"},
      [Impl("ps", _ps, _ps_parse, semantics="ps sample: %CPU is lifetime average on Linux, not instantaneous."),
       Impl("procs", lambda p, env: ["procs", "--json", "--sortd", "cpu" if p.get("sort") != "mem" else "mem",
                                     *flag(p.get("filter"), p.get("filter") or "")],
            lambda o, p: _limit(json.loads(o), {"limit": p.get("limit", 15)}), registry_id="procs",
            when=lambda p, env: None if env.supports_flag("procs", "--json") else "this procs build has no --json",
            semantics="procs --json: keys are procs column names and depend on the user's procs config.")],
      verify={"exit_in": [0], "min_records": 1}))


def _sh_sysinfo(out, p):
    info = {}
    for ln in lines(out):
        k, _, v = ln.partition("=")
        if not v:
            continue
        if k in ("MemTotal", "MemAvailable"):
            info["mem_total_bytes" if k == "MemTotal" else "mem_available_bytes"] = int(v.split()[0]) * 1024
        elif k == "cpus" and v.strip().isdigit():
            info["cpus"] = int(v)
        else:
            info[k] = v.strip()
    if "os" not in info:
        raise ValueError("no uname output")
    return info


def _sysinfo(p, env):
    import platform
    info = {"os": platform.system(), "release": platform.release(), "version": platform.version(),
            "machine": platform.machine(), "hostname": platform.node(), "cpus": os.cpu_count(), "python": platform.python_version()}
    try:
        info["loadavg"] = os.getloadavg()
    except OSError:
        pass
    osr = env.data.get("os_release") or {}
    if osr:
        info["distro"] = osr.get("PRETTY_NAME")
    mem = Path("/proc/meminfo")
    if mem.exists():
        kv = {ln.split(":")[0]: int(ln.split()[1]) * 1024 for ln in mem.read_text(encoding="utf-8").splitlines() if ln.split()[1].isdigit()}
        info["mem_total_bytes"], info["mem_available_bytes"] = kv.get("MemTotal"), kv.get("MemAvailable")
        info["swap_total_bytes"], info["swap_free_bytes"] = kv.get("SwapTotal"), kv.get("SwapFree")
    elif env.has("sysctl"):
        r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
        if r.stdout.strip().isdigit():
            info["mem_total_bytes"] = int(r.stdout)
    up = Path("/proc/uptime")
    if up.exists():
        info["uptime_s"] = float(up.read_text(encoding="utf-8").split()[0])
    return info


op(Op("system-info", "system-info", "OS, kernel, CPU count, memory, load, uptime (structured)",
      {"pretty": "bool: also run macchina for a human summary"},
      [Impl("builtin", run=_sysinfo, semantics="Read from platform, /proc and sysctl."),
       Impl("sh", lambda p, env: ["sh", "-c", 'echo "os=$(uname -s)"; echo "release=$(uname -r)"; echo "machine=$(uname -m)"; '
                                              'echo "hostname=$(uname -n)"; echo "cpus=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null)"; '
                                              'grep -E "^(MemTotal|MemAvailable):" /proc/meminfo 2>/dev/null | tr -s " " | sed "s/: /=/"; '
                                              '[ -r /etc/os-release ] && . /etc/os-release && echo "distro=$PRETTY_NAME"; '
                                              'command -v sw_vers >/dev/null 2>&1 && echo "distro=macOS $(sw_vers -productVersion)"; '
                                              '[ -r /proc/meminfo ] || { m=$(sysctl -n hw.memsize 2>/dev/null) && [ -n "$m" ] && echo "MemTotal=$((m / 1024)) kB"; }; '
                                              'echo "loadavg=$(cut -d" " -f1-3 /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null | tr -d "{}")"; true'], _sh_sysinfo,
            semantics="POSIX sh + uname//proc: works on remote targets; memory/load only where /proc exists."),
       Impl("macchina", lambda p, env: ["macchina"], lines, registry_id="macchina")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- data

def _jq_like(binary):
    return lambda p, env: [binary, *flag(p.get("raw"), "-r"), *flag(p.get("compact", True), "-c"),
                           *flag(p.get("slurp"), "-s"), p.get("filter", "."), *flag(p.get("file"), p.get("file") or "")]


def _json_out(out, p):
    out = out.strip()
    if p.get("raw"):
        return out.splitlines()
    vals = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    return vals[0] if len(vals) == 1 else vals


op(Op("json-query", "json-query", "Run a jq filter over JSON (file or stdin)",
      {"filter": "jq filter (.)", "file": "path (or provide stdin)", "stdin": "JSON text", "raw": "bool -r", "slurp": "bool -s"},
      [Impl("jaq", _jq_like("jaq"), _json_out, registry_id="jaq", stdin=lambda p: p.get("stdin"),
            semantics="jaq: jq-compatible for common filters; a few builtins differ."),
       Impl("jq", _jq_like("jq"), _json_out, stdin=lambda p: p.get("stdin")),
       Impl("builtin", run=lambda p, env: json.loads(Path(p["file"]).read_text(encoding="utf-8") if p.get("file") else p["stdin"]),
            when=lambda p, env: None if p.get("filter", ".") == "." else "python fallback supports only the identity filter '.'",
            semantics="Python json: identity filter only.")],
      verify={"exit_in": [0]}))


def _csv_rows(out):
    return list(csv.DictReader(io.StringIO(out)))


def _csv_builtin(p, env):
    path = p["file"]
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        sample = fh.read(65536)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        rdr = csv.reader(fh, dialect)
        header = next(rdr, [])
        cols = [{"field": h, "non_empty": 0, "numeric": 0, "min": None, "max": None, "distinct": set()} for h in header]
        n = 0
        for row in rdr:
            n += 1
            for c, v in zip(cols, row):
                if v != "":
                    c["non_empty"] += 1
                    if len(c["distinct"]) <= 1000:
                        c["distinct"].add(v)
                    try:
                        x = float(v)
                        c["numeric"] += 1
                        c["min"] = x if c["min"] is None else min(c["min"], x)
                        c["max"] = x if c["max"] is None else max(c["max"], x)
                    except ValueError:
                        pass
    for c in cols:
        d = len(c.pop("distinct"))
        c["cardinality"] = d if d <= 1000 else ">1000"
        c["type"] = "number" if c["non_empty"] and c["numeric"] == c["non_empty"] else "string"
    return {"rows": n, "columns": len(header), "delimiter": dialect.delimiter, "fields": cols}


op(Op("csv-profile", "csv-query", "Per-column statistics for a CSV file",
      {"file": "REQUIRED path"},
      [Impl("qsv", lambda p, env: ["qsv", "stats", "--cardinality", p["file"]], _csv_rows, registry_id="qsv"),
       Impl("xsv", lambda p, env: ["xsv", "stats", "--cardinality", p["file"]], _csv_rows, registry_id="xsv",
            semantics="xsv is archived upstream; output equivalent to qsv stats subset."),
       Impl("builtin", run=_csv_builtin, semantics="Python csv: sniffed dialect, exact counts, cardinality capped at 1000.")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- code / repo

def _tokei_parse(out, p):
    d = json.loads(out)
    langs = [{"language": k, "files": len(v.get("reports", [])), "code": v["code"], "comments": v["comments"], "blanks": v["blanks"]}
             for k, v in d.items() if k != "Total"]
    langs.sort(key=lambda r: -r["code"])
    t = d.get("Total", {})
    return {"languages": langs, "total": {"code": t.get("code"), "comments": t.get("comments"), "blanks": t.get("blanks")}}


def _loc_builtin(p, env):
    root = Path(p.get("path", "."))
    if _git_repo(root, env):
        r = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
        files = [root / f for f in r.stdout.splitlines()]
    else:
        files = [Path(d) / f for d, ds, fs in os.walk(root) for f in fs if "/." not in d and "node_modules" not in d]
    by = {}
    for f in files:
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            continue
        ext = f.suffix.lower() or f.name
        e = by.setdefault(ext, {"extension": ext, "files": 0, "lines": 0, "blank": 0})
        txt = data.decode("utf-8", "replace").splitlines()
        e["files"] += 1
        e["lines"] += len(txt)
        e["blank"] += sum(1 for ln in txt if not ln.strip())
    rows = sorted(by.values(), key=lambda r: -r["lines"])
    return {"by_extension": rows[:40], "total_lines": sum(r["lines"] for r in rows), "total_files": sum(r["files"] for r in rows)}


op(Op("code-stats", "code-stats", "Lines of code by language",
      {"path": ".", "exclude": "glob"},
      [Impl("tokei", lambda p, env: ["tokei", "-o", "json", *flag(p.get("exclude"), "-e", p.get("exclude") or ""), p.get("path", ".")],
            _tokei_parse, registry_id="tokei", when=lambda p, env: None if "json" in (env.version("tokei") or {}).get("text", "json") else "tokei built without JSON output",
            semantics="Language-aware: separates code, comments, blanks."),
       Impl("builtin", run=_loc_builtin, semantics="By file extension; no comment detection; git-tracked files only in repos.")],
      verify={"not_empty": True}))


def _onefetch_parse(out, p):
    return json.loads(out)


def _repo_builtin(p, env):
    g = ["git", "-C", p.get("path", ".")]

    def q(*a):
        r = subprocess.run([*g, *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    return {"branch": q("rev-parse", "--abbrev-ref", "HEAD"), "head": q("rev-parse", "--short", "HEAD"),
            "remotes": (q("remote", "-v") or "").splitlines()[:4], "commits": int(q("rev-list", "--count", "HEAD") or 0),
            "first_commit": q("log", "--reverse", "--format=%as", "--max-parents=0", "-1"), "last_commit": q("log", "-1", "--format=%as %s"),
            "top_authors": (q("shortlog", "-sn", "--no-merges", "HEAD") or "").splitlines()[:5]}


op(Op("repo-summary", "git-info", "Repository overview (branch, commits, authors, languages)",
      {"path": "."},
      [Impl("builtin", run=_repo_builtin, requires=("git",), semantics="git plumbing queries."),
       Impl("onefetch", lambda p, env: ["onefetch", "-o", "json", p.get("path", ".")], _onefetch_parse, registry_id="onefetch")],
      verify={"not_empty": True}))


def _status_parse(out, p):
    res = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "changed": [], "untracked": []}
    for ln in out.splitlines():
        if ln.startswith("# branch.head "):
            res["branch"] = ln.split(" ", 2)[2]
        elif ln.startswith("# branch.upstream "):
            res["upstream"] = ln.split(" ", 2)[2]
        elif ln.startswith("# branch.ab "):
            a, b = ln.split()[2:4]
            res["ahead"], res["behind"] = int(a), -int(b)
        elif ln[:2] in ("1 ", "2 "):
            f = ln.split(" ")
            res["changed"].append({"xy": f[1], "path": ln.split("\t")[0].split(" ")[-1] if ln[0] == "2" else " ".join(f[8:])})
        elif ln.startswith("u "):
            res["changed"].append({"xy": "UU", "path": ln.split(" ")[-1]})
        elif ln.startswith("? "):
            res["untracked"].append(ln[2:])
    res["clean"] = not res["changed"] and not res["untracked"]
    return res


op(Op("git-status", "vcs", "Branch, upstream divergence, changed and untracked files",
      {"path": "."},
      [Impl("git", lambda p, env: ["git", "-C", p.get("path", "."), "status", "--porcelain=v2", "--branch"], _status_parse)],
      verify={"exit_in": [0]}))


def _log_parse(out, p):
    rows = []
    for ln in lines(out):
        h, d, a, s = (ln.split("\x1f") + ["", "", "", ""])[:4]
        rows.append({"commit": h, "date": d, "author": a, "subject": s})
    return rows


op(Op("git-log", "vcs", "Recent commits (optionally for a path or range)",
      {"path": ".", "range": "rev range (HEAD)", "n": "count (20)", "file": "limit to a file"},
      [Impl("git", lambda p, env: ["git", "-C", p.get("path", "."), "log", f"-{p.get('n', 20)}", "--format=%h\x1f%as\x1f%an\x1f%s",
                                   p.get("range", "HEAD"), *flag(p.get("file"), "--", p.get("file") or "")], _log_parse)],
      verify={"exit_in": [0]}))


def _numstat_parse(out, p):
    rows = []
    for ln in lines(out):
        a, d, path = ln.split("\t", 2)
        rows.append({"path": path, "added": None if a == "-" else int(a), "deleted": None if d == "-" else int(d)})
    return {"files": rows, "files_changed": len(rows), "added": sum(r["added"] or 0 for r in rows),
            "deleted": sum(r["deleted"] or 0 for r in rows)}


op(Op("git-diff-stat", "vcs", "Per-file added/deleted lines for a range (default: working tree vs HEAD)",
      {"path": ".", "range": "e.g. main...HEAD (default HEAD)", "staged": "bool"},
      [Impl("git", lambda p, env: ["git", "-C", p.get("path", "."), "diff", "--numstat", *flag(p.get("staged"), "--cached"),
                                   *([p["range"]] if p.get("range") else ["HEAD"])], _numstat_parse)],
      verify={"exit_in": [0]}))


def _difft(p, env):
    if p.get("range") is not None:
        return ["git", "-C", p.get("path", "."), "-c", "diff.external=difft", "diff", "--ext-diff", "--no-color", p.get("range") or "HEAD"]
    return ["difft", "--color", "never", "--display", "inline", p["a"], p["b"]]


op(Op("diff", "diff-structural", "Diff two files, or a git range (structural when difftastic is available)",
      {"a": "file A", "b": "file B", "range": "git range instead of files (e.g. HEAD~1)", "path": "repo dir for range mode"},
      [Impl("difft", _difft, lines, registry_id="difftastic", ok_exit=(0, 1),
            semantics="Structural: formatting-only edits are hidden; output is not a patch."),
       Impl("git", lambda p, env: (["git", "-C", p.get("path", "."), "diff", "--no-color", "--stat", "-p", p.get("range") or "HEAD"]
                                   if p.get("range") is not None else ["git", "diff", "--no-index", "--no-color", p["a"], p["b"]]),
            lines, ok_exit=(0, 1), semantics="Line-based unified diff."),
       Impl("diff", lambda p, env: ["diff", "-u", p["a"], p["b"]] if p.get("range") is None else None, lines, ok_exit=(0, 1),
            semantics="Line-based unified diff.")],
      verify={"exit_in": [0, 1]}))


# ----------------------------------------------------------------------------- performance

def _hyperfine(p, env):
    out = os.path.join(p.get("_run_dir") or tempfile.gettempdir(), f"hyperfine-{os.getpid()}-{time.time_ns()}.json")
    p["_export"] = out
    a = ["hyperfine", "--style", "basic", "--export-json", out, "--warmup", str(p.get("warmup", 1))]
    a += ["--runs", str(p["runs"])] if p.get("runs") else []
    a += flag(p.get("prepare"), "--prepare", p.get("prepare") or "")
    a += flag(p.get("no_shell"), "-N")
    a += flag(p.get("ignore_failure"), "-i")
    return a + list(_cmds(p))


def _cmds(p):
    c = p["commands"]
    return [c] if isinstance(c, str) else c


def _hyperfine_parse(out, p):
    data = json.loads(Path(p["_export"]).read_text(encoding="utf-8"))
    os.unlink(p["_export"])
    return {"results": [{k: r.get(k) for k in ("command", "mean", "stddev", "median", "min", "max", "user", "system")} | {"runs": len(r.get("times", [])), "exit_codes": sorted(set(r.get("exit_codes") or []))}
                        for r in data["results"]], "unit": "seconds"}


def _bench_builtin(p, env):
    import statistics
    res = []
    for c in _cmds(p):
        for _ in range(int(p.get("warmup", 1))):
            subprocess.run(sandbox.shell_argv(c), capture_output=True)
        times, codes = [], set()
        for _ in range(int(p.get("runs") or 10)):
            if p.get("prepare"):
                subprocess.run(sandbox.shell_argv(p["prepare"]), capture_output=True)
            t = time.perf_counter()
            r = subprocess.run(sandbox.shell_argv(c), capture_output=True)
            times.append(time.perf_counter() - t)
            codes.add(r.returncode)
        res.append({"command": c, "mean": statistics.mean(times), "stddev": statistics.stdev(times) if len(times) > 1 else None,
                    "median": statistics.median(times), "min": min(times), "max": max(times), "runs": len(times), "exit_codes": sorted(codes)})
    return {"results": res, "unit": "seconds"}


op(Op("benchmark", "benchmark", "Benchmark one or more shell commands (mean/stddev/min/max)",
      {"commands": "REQUIRED command string or list", "runs": "exact runs (hyperfine auto if unset; fallback 10)",
       "warmup": "warmup runs (1)", "prepare": "command before each run", "no_shell": "bool -N", "ignore_failure": "bool"},
      [Impl("hyperfine", _hyperfine, _hyperfine_parse, registry_id="hyperfine",
            semantics="Shell spawn time is calibrated out unless -N."),
       Impl("builtin", run=_bench_builtin, semantics="Python loop: includes shell spawn overhead, no outlier detection; indicative only.")],
      risk=RUNS_CODE, timeout=900, verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- replace (preview / apply)

def _rg_replace_preview(p, env):
    return ["rg", "-n", "-0", "--with-filename", "--no-heading", "--color", "never", *flag(p.get("fixed"), "-F"),
            *flag(p.get("glob"), "-g", p.get("glob") or ""), "-r", p["replacement"], "-e", p["pattern"], "--", p.get("path", ".")]


def _rg_replace_parse(out, p):
    rows = []
    for ln in lines(out):
        path, _, rest = ln.partition("\0")
        num, _, text = rest.partition(":")
        rows.append({"path": path, "line": int(num), "after": text[:400]})
    return {"changes": _limit(rows, {"limit": p.get("limit", 200)}), "change_count": len(rows),
            "files": sorted({r["path"] for r in rows})}


op(Op("replace-preview", "search-replace", "Show what a find/replace would change; writes nothing",
      {"pattern": "REQUIRED regex", "replacement": "REQUIRED ($1 for captures)", "path": ".", "fixed": "bool", "glob": "file glob"},
      [Impl("rg", _rg_replace_preview, _rg_replace_parse, registry_id="ripgrep", ok_exit=(0, 1),
            semantics="rg --replace prints replaced lines only; files untouched.")],
      verify={"exit_in": [0, 1]}))


def _files_arg(p):
    f = p.get("files") or []
    return f if isinstance(f, list) else [f]


op(Op("replace-apply", "search-replace", "Apply a find/replace to an explicit file list (from replace-preview)",
      {"pattern": "REQUIRED", "replacement": "REQUIRED", "files": "REQUIRED list of files", "fixed": "bool"},
      [Impl("sd", lambda p, env: ["sd", *flag(p.get("fixed"), "-F"), "--", p["pattern"], p["replacement"], *_files_arg(p)] if _files_arg(p) else None,
            registry_id="sd", semantics="Rust regex identical to rg preview ($1 captures)."),
       Impl("perl", lambda p, env: ["perl", "-pi", "-e", _perl_expr(p), *_files_arg(p)] if _files_arg(p) and _perl_expr(p) else None,
            semantics="Perl regex: equivalent for common patterns; refuses $-captures it cannot map safely.")],
      risk=WRITES, verify={"exit_in": [0]}, undo=lambda p: {"modify": _files_arg(p)}))


def _perl_expr(p):
    pat, rep = p["pattern"], p["replacement"]
    if p.get("fixed"):
        pat, rep = re.escape(pat), rep.replace("$", "\\$").replace("@", "\\@")
    elif re.search(r"\$\{?[A-Za-z_]", rep) or "@" in rep:  # named captures/arrays differ between engines
        return None
    if "#" in pat or "#" in rep:
        return None
    return f"s#{pat}#{rep}#g"


# ----------------------------------------------------------------------------- files: archive / copy / delete

def _arch_kind(path):
    for ext, k in ((".tar.gz", "tar"), (".tgz", "tar"), (".tar.xz", "tar"), (".tar.zst", "tar"), (".tar.bz2", "tar"), (".tar", "tar"), (".zip", "zip")):
        if path.lower().endswith(ext):
            return k
    return None


op(Op("archive-list", "archive", "List archive contents",
      {"archive": "REQUIRED path"},
      [Impl("ouch", lambda p, env: ["ouch", "list", p["archive"]], lines, registry_id="ouch"),
       Impl("tar", lambda p, env: ["tar", "-tf", p["archive"]] if _arch_kind(p["archive"]) == "tar" else None, lines),
       Impl("unzip", lambda p, env: ["unzip", "-Z1", p["archive"]] if _arch_kind(p["archive"]) == "zip" else None, lines)],
      verify={"exit_in": [0], "min_records": 1}))

op(Op("archive-extract", "archive", "Extract an archive into a NEW destination directory (never overwrites)",
      {"archive": "REQUIRED", "dest": "REQUIRED directory (must not exist or be empty)"},
      [Impl("ouch", lambda p, env: ["ouch", "decompress", "--no", "-d", p["dest"], p["archive"]], registry_id="ouch"),
       Impl("tar", lambda p, env: ["tar", "-xf", p["archive"], "-C", p["dest"], "-k"] if _arch_kind(p["archive"]) == "tar" else None,
            semantics="tar -k keeps existing files."),
       Impl("unzip", lambda p, env: ["unzip", "-n", p["archive"], "-d", p["dest"]] if _arch_kind(p["archive"]) == "zip" else None)],
      risk=WRITES, verify={"exit_in": [0], "path_exists": "${dest}"}, undo=lambda p: {"create": [p["dest"]]}))

op(Op("copy", "copy-files", "Copy files/trees without overwriting existing destination files",
      {"src": "REQUIRED (str or list)", "dest": "REQUIRED"},
      [Impl("xcp", lambda p, env: ["xcp", "-r", "--no-clobber", *_srcs(p), p["dest"]], registry_id="xcp"),
       Impl("cp", lambda p, env: ["cp", "-R", "-n", *_srcs(p), p["dest"]], semantics="cp -n silently skips existing files.")],
      risk=WRITES, verify={"exit_in": [0], "path_exists": "${dest}"}, undo=lambda p: {"create": _copy_targets(p)}))


def _copy_targets(p):
    if os.path.isdir(p["dest"]):
        return [os.path.join(p["dest"], os.path.basename(str(s).rstrip("/"))) for s in _srcs(p)]
    return [p["dest"]]


def _srcs(p):
    s = p["src"]
    return [s] if isinstance(s, str) else s


op(Op("safe-delete", "safe-delete", "Move paths to a recoverable trash. There is deliberately no rm fallback.",
      {"paths": "REQUIRED (str or list)"},
      [Impl("trash", lambda p, env: ["trash", "put", *_list(p["paths"])], registry_id="trashy",
            when=lambda p, env: None if re.search(r"^\s+put\b", env.help_text("trash"), re.M) else "`trash` on PATH is not trashy (no `put` subcommand)"),
       Impl("rip", lambda p, env: ["rip", *_list(p["paths"])], registry_id="rip", semantics="Moves to rip graveyard; restore with `rip -u`."),
       Impl("gio", lambda p, env: ["gio", "trash", *_list(p["paths"])], semantics="Freedesktop trash."),
       Impl("trash-put", lambda p, env: ["trash-put", *_list(p["paths"])], semantics="trash-cli (Freedesktop trash).")],
      risk=WRITES, verify={"exit_in": [0], "path_absent": "${paths}"}, undo=lambda p: {"deleted": _list(p["paths"])}))


def _list(v):
    return [v] if isinstance(v, str) else list(v)


# ----------------------------------------------------------------------------- network

op(Op("net-connections", "network-monitor", "Open sockets with owning processes (own processes unless root)",
      {"listening": "bool: only listening sockets"},
      [Impl("ss", lambda p, env: ["ss", "-tunp", *(["-l"] if p.get("listening") else ["-a"])], lines),
       Impl("lsof", lambda p, env: ["lsof", "-nP", "-i", *flag(p.get("listening"), "-sTCP:LISTEN")], lines, ok_exit=(0, 1)),
       Impl("netstat", lambda p, env: ["netstat", "-an"], lines)],
      verify={"exit_in": [0, 1]}))


def _mtr_parse(out, p):
    i = out.find("{")
    if i < 0:
        raise ValueError("mtr produced no JSON (older mtr without --json support?)")
    d = json.loads(out[i:])
    hubs = (d.get("report") or {}).get("hubs", [])
    return {"target": p["target"], "hops": hubs}


def _trip_parse(out, p):
    d = json.loads(out)
    hops = d.get("hops", d)
    return {"target": p["target"], "hops": hops}


op(Op("trace-route", "traceroute", "Path, per-hop latency and loss to a target",
      {"target": "REQUIRED host/IP", "cycles": "probe rounds (5)", "max_hops": "30"},
      [Impl("trip", lambda p, env: ["trip", "-m", "json", "-C", str(p.get("cycles", 5)), *flag(env.os != "linux" or not env.data["privileges"]["is_root"], "-u"),
                                    p["target"]], _trip_parse, registry_id="trippy",
            semantics="ICMP; unprivileged mode (-u) where raw sockets are unavailable."),
       Impl("mtr", lambda p, env: ["mtr", "--report", "--json", "-c", str(p.get("cycles", 5)), p["target"]], _mtr_parse),
       Impl("traceroute", lambda p, env: ["traceroute", "-n", "-q", "1", "-w", "2", "-m", str(p.get("max_hops", 30)), p["target"]], lines,
            semantics="Single probe per hop: no loss statistics."),
       Impl("tracepath", lambda p, env: ["tracepath", "-n", p["target"]], lines, semantics="No loss statistics.")],
      risk=NETWORK, timeout=180, verify={"exit_in": [0], "not_empty": True}))

op(Op("port-scan", "port-scan", "TCP connect scan of an AUTHORIZED target (requires authorized_scope)",
      {"target": "REQUIRED", "ports": "e.g. 22,80,443 or 1-1024", "authorized_scope": "REQUIRED: target/CIDR the user authorized"},
      [Impl("rustscan", lambda p, env: ["rustscan", "-a", p["target"], "-g", *flag(p.get("ports"), "-p" if "," in str(p.get("ports")) or str(p.get("ports")).isdigit() else "-r", str(p.get("ports"))),
                                        "--ulimit", "4096"], lines, registry_id="rustscan"),
       Impl("nmap", lambda p, env: ["nmap", "-Pn", "-sT", "-oG", "-", *flag(p.get("ports"), "-p", str(p.get("ports"))), p["target"]], lines)],
      risk=NETWORK, timeout=600, verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- automation / help

def _task_builtin(p, env):
    root = Path(p.get("path", "."))
    found = {}
    if (root / "justfile").exists() or (root / "Justfile").exists() or (root / ".justfile").exists():
        if env.has("just"):
            r = subprocess.run(["just", "--justfile", str(next(f for f in (root / "justfile", root / "Justfile", root / ".justfile") if f.exists())), "--summary"],
                               capture_output=True, text=True)
            found["just"] = r.stdout.split() if r.returncode == 0 else {"error": r.stderr.strip()[:300]}
        else:
            found["just"] = "justfile present but `just` is not installed"
    if (root / "maskfile.md").exists():
        found["mask"] = re.findall(r"^##\s+(\S+)", (root / "maskfile.md").read_text(encoding="utf-8"), re.M)
    if (root / "Makefile").exists():
        found["make"] = sorted(set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", (root / "Makefile").read_text(encoding="utf-8"), re.M)) - {".PHONY"})
    if (root / "package.json").exists():
        found["npm"] = list((json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {}).keys())
    if (root / "Cargo.toml").exists():
        found["cargo"] = ["build", "test", "clippy", "fmt --check"]
    for f in ("mise.toml", ".mise.toml"):
        if (root / f).exists():
            found["mise"] = re.findall(r"^\[tasks\.([^\]]+)\]", (root / f).read_text(encoding="utf-8"), re.M)
    return found


op(Op("task-list", "task-runner", "Discover project task runners and their recipes (just, mask, make, npm, cargo, mise)",
      {"path": "."},
      [Impl("builtin", run=_task_builtin, semantics="Reads task files; runs only `just --summary`.")],
      verify={}))


def _parallel_builtin(p, env):
    import concurrent.futures as cf
    cmds = _cmds(p)

    wrapped = {c: sandbox.shell_argv(c) for c in cmds}  # wrap in this thread: sandbox settings are thread-local

    def one(c):
        t = time.perf_counter()
        try:
            r = subprocess.run(wrapped[c], capture_output=True, text=True, timeout=int(p.get("timeout", 300)))
            return {"command": c, "exit": r.returncode, "seconds": round(time.perf_counter() - t, 3),
                    "stdout_tail": r.stdout[-2000:], "stderr_tail": r.stderr[-2000:]}
        except subprocess.TimeoutExpired:
            return {"command": c, "exit": None, "timeout": True}

    with cf.ThreadPoolExecutor(max_workers=int(p.get("jobs", os.cpu_count() or 4))) as ex:
        res = list(ex.map(one, cmds))
    return {"results": res, "failed": [r["command"] for r in res if r.get("exit") != 0], "total": len(res)}


op(Op("parallel", "parallel-exec", "Run independent shell commands concurrently with per-command exit codes",
      {"commands": "REQUIRED list", "jobs": "concurrency (CPU count)", "timeout": "per command seconds (300)"},
      [Impl("builtin", run=_parallel_builtin, semantics="Per-command exit code/stdout/stderr kept separately."),
       Impl("rust-parallel", lambda p, env: ["rust-parallel", "-s", "-j", str(p.get("jobs", os.cpu_count() or 4))], lines,
            stdin=lambda p: "\n".join(_cmds(p)) + "\n", registry_id="rust-parallel",
            semantics="Interleaved output; overall exit code only.")],
      risk=RUNS_CODE, timeout=1800, verify={"no_failed": True}))


op(Op("help", "tldr", "Read a tool's usage: --help, then tldr page, then man",
      {"tool": "REQUIRED binary name"},
      [Impl("help", lambda p, env: [p["tool"], "--help"] if env.has(p["tool"]) else None, lambda o, p: o[:12000], ok_exit=(0, 1, 2)),
       Impl("tldr", lambda p, env: ["tldr", p["tool"]], lambda o, p: o[:8000]),
       Impl("man", lambda p, env: ["man", "-P", "cat", p["tool"]], lambda o, p: o[:12000])],
      verify={"not_empty": True}))


from . import builtins as _builtins  # noqa: E402,F401  (registers portable fallbacks)
from . import ops_extra as _extra  # noqa: E402,F401  (profiling, csv-query, http-test, ...)
from . import ops_git as _git_ops  # noqa: E402,F401  (gated git writes with safe undo)
from . import ops_more as _more  # noqa: E402,F401  (grex, fend, lemmeknow, pastel, task-run, qsv, zola, ...)


def describe(o):
    return {"op": o.name, "capability": o.capability, "summary": o.summary, "risk": o.risk, "params": o.params,
            "implementations": [{"tool": i.tool, "registry_id": i.registry_id, "semantics": i.semantics or None} for i in o.impls]}


def ops_for_capability(cap):
    return [o for o in OPS.values() if o.capability == cap]
