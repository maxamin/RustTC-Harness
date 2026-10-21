"""Additional ops: profiling, CSV SQL, HTTP tests, filesystem SQL, syscall tracing, bandwidth, resource history."""
import csv
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ops as O
from . import sandbox
from .ops import Impl, Op, op, flag, lines, NETWORK, PRIVILEGED, READ_ONLY, RUNS_CODE, WRITES


def _out_path(p, name, suffix):
    """Artifacts go to the run directory (writable inside the sandbox) unless the caller set `output`."""
    if p.get("output"):
        return p["output"]
    p["output"] = os.path.join(p.get("_run_dir") or tempfile.gettempdir(), f"{name}-{time.time_ns()}{suffix}")
    return p["output"]


def _argv(cmd):
    return shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]


def _csv(out, p=None):
    return list(csv.DictReader(io.StringIO(out)))


# ----------------------------------------------------------------------------- csv-query (SQL)

def _sql_readonly(sql):
    if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        raise ValueError("csv-query accepts a single SELECT/WITH statement")
    return sql.strip().rstrip(";")


def _csv_sqlite(p, env):
    sql = _sql_readonly(p["sql"])
    con = sqlite3.connect(":memory:")
    with open(p["file"], newline="", encoding="utf-8", errors="replace") as fh:
        sample = fh.read(65536)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        rdr = csv.reader(fh, dialect)
        header = next(rdr)
        cols = ", ".join(f'"{h}"' for h in header)
        con.execute(f"CREATE TABLE data ({cols})")
        con.executemany(f"INSERT INTO data VALUES ({', '.join('?' * len(header))})",
                        (r + [None] * (len(header) - len(r)) for r in rdr))

    def guard(action, *args):  # read-only even if the regex check were bypassed
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    con.set_authorizer(guard)
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchmany(int(p.get("limit", 1000)))]
    return {"rows": rows, "columns": names, "row_count": len(rows)}


op(Op("csv-query", "csv-query", "Run a read-only SQL SELECT over a CSV file (table name: data)",
      {"file": "REQUIRED csv path", "sql": "REQUIRED single SELECT, e.g. SELECT city, count(*) FROM data GROUP BY city", "limit": "max rows (1000)"},
      [Impl("qsv", lambda p, env: ["qsv", "sqlp", p["file"], re.sub(r"\bdata\b", "_t_1", _sql_readonly(p["sql"]))],
            lambda o, p: {"rows": _csv(o)[: int(p.get("limit", 1000))], "row_count": len(_csv(o))}, registry_id="qsv",
            when=lambda p, env: None if "sqlp" in env.help_text("qsv") else "this qsv build has no sqlp (Polars) feature",
            semantics="qsv sqlp (Polars SQL): values are typed by inference; `data` is rewritten to _t_1."),
       Impl("builtin", run=_csv_sqlite, semantics="sqlite3 in memory: all columns TEXT (CAST for numeric comparisons); read-only authorizer.")],
      verify={"not_empty": True}))


# ----------------------------------------------------------------------------- fs-query (fselect)

op(Op("fs-query", "filesystem-sql", "SQL-like file query via fselect (e.g. fields 'path, size', where 'size gt 1M')",
      {"path": ".", "fields": "path, size, modified", "where": "fselect where clause", "order": "e.g. size desc", "limit": "rows (100)"},
      [Impl("fselect", lambda p, env: ["fselect", " ".join(filter(None, [
          "select", p.get("fields", "path, size, modified"), "from", p.get("path", "."),
          f"where {p['where']}" if p.get("where") else "", f"order by {p['order']}" if p.get("order") else "",
          f"limit {p.get('limit', 100)}", "into json"]))], json.loads, registry_id="fselect",
            semantics="fselect query language (not SQL-92); see `rth info fselect`.")],
      verify={"exit_in": [0]}))


# ----------------------------------------------------------------------------- http-test

def _hurl_parse(out, p):
    res = [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]
    return {"results": res, "failed": [r.get("filename") for r in res if not r.get("success", True)]}


def _http_builtin(p, env):
    req = urllib.request.Request(p["url"], method=p.get("method", "GET"), headers={"User-Agent": "rth-http-test"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=int(p.get("timeout", 15))) as r:
            status, body = r.status, r.read(1 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read(1 << 16).decode("utf-8", "replace")
    ms = round((time.monotonic() - t0) * 1000, 1)
    checks = [{"assert": f"status == {p.get('expect_status', 200)}", "ok": status == int(p.get("expect_status", 200))}]
    if p.get("expect_body"):
        checks.append({"assert": f"body =~ {p['expect_body']}", "ok": re.search(p["expect_body"], body) is not None})
    return {"results": [{"url": p["url"], "status": status, "ms": ms, "checks": checks}],
            "failed": [c["assert"] for c in checks if not c["ok"]]}


op(Op("http-test", "http-test", "HTTP assertions: a .hurl file (hurl --test) or a URL with expected status/body regex",
      {"file": ".hurl file", "url": "URL (instead of file)", "expect_status": "200", "expect_body": "regex", "method": "GET"},
      [Impl("hurl", lambda p, env: ["hurl", "--test", "--json", p["file"]] if p.get("file") else None, _hurl_parse,
            registry_id="hurl", ok_exit=(0, 3, 4), semantics="hurl: full assertion language; exit 4 = assertion failed."),
       Impl("builtin", run=_http_builtin, when=lambda p, env: None if p.get("url") else "needs url",
            semantics="urllib: single request, status and body-regex assertions only.")],
      risk=NETWORK, timeout=120, verify={"no_failed": True}))


# ----------------------------------------------------------------------------- profiling

def _perf_inferno(p, env):
    out = Path(_out_path(p, "flamegraph", ".svg"))
    with tempfile.TemporaryDirectory(dir=p.get("_run_dir")) as td:
        data = os.path.join(td, "perf.data")
        r = subprocess.run(sandbox.argv(["perf", "record", "-F", str(p.get("frequency", 99)), "-g", "-o", data, "--", *_argv(p["command"])]),
                           capture_output=True, text=True, timeout=int(p.get("timeout", 600)))
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-800:])
        script = subprocess.run(["perf", "script", "-i", data], capture_output=True, text=True)
        folded = subprocess.run(["inferno-collapse-perf"], input=script.stdout, capture_output=True, text=True)
        svg = subprocess.run(["inferno-flamegraph"], input=folded.stdout, capture_output=True, text=True)
        out.write_text(svg.stdout, encoding="utf-8")
    return {"flamegraph": str(out), "bytes": out.stat().st_size, "samples_lines": folded.stdout.count("\n")}


op(Op("cpu-profile", "cpu-profile", "Sample a command's CPU stacks into a flamegraph SVG",
      {"command": "REQUIRED command (str or list)", "output": "svg path (temp file if unset)", "frequency": "Hz (99)"},
      [Impl("flamegraph", lambda p, env: ["flamegraph", "-o", _out_path(p, "flamegraph", ".svg"),
                                          "-F", str(p.get("frequency", 99)), "--", *_argv(p["command"])],
            lambda o, p: {"flamegraph": p["output"], "bytes": Path(p["output"]).stat().st_size}, registry_id="cargo-flamegraph",
            semantics="cargo-flamegraph: perf (Linux) / dtrace (macOS); may need perf_event_paranoid <= 1."),
       Impl("builtin", run=_perf_inferno, requires=("perf", "inferno-collapse-perf", "inferno-flamegraph"),
            semantics="perf record -g | inferno: same data as flamegraph, rendered by inferno.")],
      risk=RUNS_CODE, timeout=900, verify={"not_empty": True}))


def _cprofile(p, env):
    cmd = _argv(p["command"])
    if cmd and Path(cmd[0]).name.startswith("python"):
        cmd = cmd[1:]
    if not cmd:
        raise ValueError("command must be a python script/module invocation")
    with tempfile.TemporaryDirectory(dir=p.get("_run_dir")) as td:
        prof = os.path.join(td, "out.prof")
        r = subprocess.run(sandbox.argv([sys.executable, "-m", "cProfile", "-o", prof, *cmd]), capture_output=True, text=True,
                           timeout=int(p.get("timeout", 600)))
        if not os.path.exists(prof):
            raise RuntimeError(r.stderr[-800:] or "profiling produced no output")
        import pstats
        st = pstats.Stats(prof)
        rows = []
        for (f, ln, fn), (cc, nc, tt, ct, _) in st.stats.items():
            rows.append({"function": f"{fn} ({os.path.basename(f)}:{ln})", "calls": nc, "self_s": round(tt, 6), "cumulative_s": round(ct, 6)})
    rows.sort(key=lambda r: -r["cumulative_s"])
    return {"results": rows[: int(p.get("top", 25))], "exit": r.returncode}


op(Op("python-profile", "python-profile", "Profile a Python program: py-spy flamegraph/speedscope, or cProfile top functions",
      {"command": "REQUIRED e.g. 'python app.py --flag' (or pid via py-spy)", "pid": "attach to running process (py-spy)",
       "duration": "seconds when attaching (10)", "output": "py-spy output file", "top": "rows (25)"},
      [Impl("py-spy", lambda p, env: ["py-spy", "record", "-f", "speedscope", "-o", _out_path(p, "py-spy", ".json"),
                                      *(["--pid", str(p["pid"]), "-d", str(p.get("duration", 10))] if p.get("pid") else ["--", *_argv(p["command"])])],
            lambda o, p: {"profile": p["output"], "format": "speedscope", "bytes": Path(p["output"]).stat().st_size},
            registry_id="py-spy", semantics="py-spy sampling: low overhead, includes native frames; attaching needs ptrace permission."),
       Impl("builtin", run=_cprofile, semantics="cProfile (deterministic): higher overhead, Python frames only; top functions by cumulative time.")],
      risk=RUNS_CODE, timeout=900, verify={"not_empty": True}))


# ----------------------------------------------------------------------------- syscall-trace

def _strace_c(p, env):
    with tempfile.TemporaryDirectory(dir=p.get("_run_dir")) as td:
        f = os.path.join(td, "s.txt")
        r = subprocess.run(sandbox.argv(["strace", "-f", "-c", "-o", f, *_argv(p["command"])]), capture_output=True, text=True,
                           timeout=int(p.get("timeout", 300)))
        txt = Path(f).read_text(encoding="utf-8") if os.path.exists(f) else ""
    rows = []
    for ln in txt.splitlines():
        m = re.match(r"\s*([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(?:(\d+)\s+)?(\w+)$", ln)
        if m and m[6] != "total":
            rows.append({"syscall": m[6], "pct_time": float(m[1]), "seconds": float(m[2]), "calls": int(m[4]), "errors": int(m[5] or 0)})
    if not rows:
        raise RuntimeError(r.stderr[-600:] or "strace produced no summary")
    return {"results": rows, "exit": r.returncode}


op(Op("syscall-trace", "syscall-trace", "Summarize a command's system calls (counts, time, errors)",
      {"command": "REQUIRED command (str or list)"},
      [Impl("lurk", lambda p, env: ["lurk", "-c", "-f", "--", *_argv(p["command"])], lines, registry_id="lurk",
            semantics="lurk summary (x86_64 Linux)."),
       Impl("builtin", run=_strace_c, requires=("strace",), semantics="strace -f -c summary table, parsed.")],
      risk=RUNS_CODE, timeout=600, verify={"not_empty": True}))


# ----------------------------------------------------------------------------- net-bandwidth

def _timed(argv, seconds):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=seconds)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:  # expected: these tools run until stopped
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return out, err, 0


def _bandwhich(p, env):
    out, err, code = _timed([env.which("bandwhich"), "--raw", "-n"], int(p.get("seconds", 5)))
    if not out.strip():
        raise PermissionError(err.strip()[-400:] or "bandwhich produced no output (needs CAP_NET_RAW/root)")
    return {"lines": out.splitlines()[-200:]}


def _proc_net_dev(p, env):
    f = Path("/proc/net/dev")
    if not f.exists():
        raise RuntimeError("/proc/net/dev not available (not Linux)")

    def read():
        d = {}
        for ln in f.read_text(encoding="utf-8").splitlines()[2:]:
            name, rest = ln.split(":", 1)
            v = rest.split()
            d[name.strip()] = (int(v[0]), int(v[8]))
        return d

    a = read()
    secs = float(p.get("seconds", 5))
    time.sleep(secs)
    b = read()
    rows = [{"interface": k, "rx_bytes_per_s": round((b[k][0] - a[k][0]) / secs), "tx_bytes_per_s": round((b[k][1] - a[k][1]) / secs)}
            for k in b if k in a]
    rows.sort(key=lambda r: -(r["rx_bytes_per_s"] + r["tx_bytes_per_s"]))
    return rows


op(Op("net-bandwidth", "network-bandwidth", "Network throughput over N seconds: per process (bandwhich, privileged) or per interface",
      {"seconds": "sample duration (5)"},
      [Impl("builtin", run=_bandwhich, requires=("bandwhich",), registry_id="bandwhich",
            semantics="bandwhich --raw: per process/connection; needs CAP_NET_RAW or root.", risk=PRIVILEGED),
       Impl("builtin", run=_proc_net_dev, semantics="/proc/net/dev deltas: per interface only (no process attribution), unprivileged, Linux.")],
      timeout=120, verify={"min_records": 1}))


# ----------------------------------------------------------------------------- resource-history

op(Op("resource-history", "resource-history", "Historical system metrics (what happened earlier) from below or sar",
      {"begin": "below time spec (e.g. '-1h') / sar start HH:MM:SS", "end": "optional end"},
      [Impl("below", lambda p, env: ["below", "dump", "system", "-b", str(p.get("begin", "-1h")), *flag(p.get("end"), "-e", str(p.get("end"))), "-O", "json"],
            json.loads, registry_id="below", semantics="below: needs a running `below record` and root to read its store."),
       Impl("sar", lambda p, env: ["sar", "-u", "-r", *flag(":" in str(p.get("begin", "")), "-s", str(p.get("begin")))],
            lines, semantics="sysstat sar: needs sadc collection enabled; text output.")],
      verify={"not_empty": True}))
