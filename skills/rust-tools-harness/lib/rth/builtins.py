"""Portable pure-Python fallbacks (last in each op's ranking).

They make every core op work on machines without the usual tools (minimal containers, Windows).
Each states its semantic differences, like any other implementation. Registered onto OPS at import.
"""
import difflib
import fnmatch
import os
import re
import shutil
import stat
import tarfile
import time
import zipfile
from pathlib import Path

from . import ops as O

UNITS = {"": 1, "b": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12, "ki": 2**10, "mi": 2**20, "gi": 2**30, "ti": 2**40}


def parse_size(v):
    """fd-compatible: '10M' = 10*10^6 (SI); '10Mi' = 10*2^20."""
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*([kmgt]i?|b)?b?", str(v).strip(), re.I)
    if not m:
        raise ValueError(f"bad size {v!r}")
    return int(float(m[1]) * UNITS[(m[2] or "").lower()])


def parse_age(v):
    m = re.fullmatch(r"(\d+)\s*(s|min|m|h|d|w)?", str(v).strip(), re.I)
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return int(m[1]) * {"s": 1, "min": 60, "m": 60, "h": 3600, "d": 86400, "w": 604800}[(m[2] or "d").lower()]


def _walk(root, *, hidden=False, max_depth=None):
    root = Path(root)
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        depth = len(Path(d).parts) - base
        dirs[:] = [x for x in dirs if x != ".git" and (hidden or not x.startswith("."))]
        if max_depth is not None and depth + 1 >= int(max_depth):
            for x in dirs:
                yield Path(d) / x, True
            dirs[:] = []
        else:
            for x in dirs:
                yield Path(d) / x, True
        for f in files:
            if hidden or not f.startswith("."):
                yield Path(d) / f, False


def find_files(p, env):
    pat, ext, typ = p.get("pattern") or "*", p.get("ext"), p.get("type")
    min_size = parse_size(p["min_size"]) if p.get("min_size") else None
    cutoff = time.time() - parse_age(p["changed_within"]) if p.get("changed_within") else None
    rows = []
    for path, is_dir in _walk(p.get("path", "."), hidden=p.get("hidden"), max_depth=p.get("max_depth")):
        if typ == "f" and is_dir or typ == "d" and not is_dir or typ == "l" and not path.is_symlink():
            continue
        if ext and not path.name.endswith("." + ext) or not fnmatch.fnmatch(path.name, pat):
            continue
        if min_size is not None or cutoff is not None:
            try:
                st = path.lstat()
            except OSError:
                continue
            if min_size is not None and (is_dir or st.st_size < min_size) or cutoff is not None and st.st_mtime < cutoff:
                continue
        rows.append({"path": str(path)})
        if p.get("limit") and len(rows) >= int(p["limit"]):
            break
    return rows


def _is_binary(path):
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(8000)
    except OSError:
        return True


def search_content(p, env):
    flags = re.I if p.get("ignore_case") else 0
    pat = re.escape(p["pattern"]) if p.get("fixed") else p["pattern"]
    if p.get("word"):
        pat = rf"\b(?:{pat})\b"
    rx = re.compile(pat, flags)
    root = Path(p.get("path", "."))
    files = [root] if root.is_file() else [f for f, d in _walk(root, hidden=p.get("hidden")) if not d]
    rows, limit, per = [], int(p.get("limit") or 500), p.get("max_count")
    for f in files:
        if p.get("glob") and not fnmatch.fnmatch(f.name, p["glob"]) or _is_binary(f):
            continue
        n = 0
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        rows.append({"path": str(f), "line": i, "text": line.rstrip("\n")[:400]})
                        n += 1
                        if per and n >= int(per):
                            break
        except OSError:
            continue
    return {"matches": rows[:limit], "match_count": len(rows), "files": sorted({r["path"] for r in rows}), "truncated": len(rows) > limit}


def view_file(p, env):
    s, e = O._range(p)
    out = []
    with open(p["path"], encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i > e:
                break
            if i >= s:
                out.append(f"{i:6d}  {line.rstrip(chr(10))}")
    return "\n".join(out)


def hex_view(p, env):
    with open(p["path"], "rb") as fh:
        fh.seek(int(p.get("skip", 0)))
        data = fh.read(int(p.get("length", 256)))
    off = int(p.get("skip", 0))
    rows = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(chunk[j:j + 2].hex() for j in range(0, len(chunk), 2))
        rows.append(f"{off + i:08x}: {hx:<39}  {''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)}")
    return "\n".join(rows)


def list_dir(p, env):
    rows = []
    with os.scandir(p.get("path", ".")) as it:
        for e in sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name)):
            if not p.get("all", True) and e.name.startswith("."):
                continue
            st = e.stat(follow_symlinks=False)
            rows.append({"name": e.name, "type": "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file",
                         "mode": stat.filemode(st.st_mode), "size": st.st_size,
                         "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return rows


def tree(p, env):
    root = Path(p.get("path", "."))
    return [str(root)] + sorted(str(x) for x, _ in _walk(root, max_depth=int(p.get("depth", 2))))


def _usage(path, one_fs, apparent, dev=None):
    total = 0
    for d, dirs, files in os.walk(path):
        if one_fs:
            dirs[:] = [x for x in dirs if _dev(os.path.join(d, x)) == dev]
        for f in files:
            try:
                st = os.lstat(os.path.join(d, f))
            except OSError:
                continue
            total += st.st_size if apparent or not hasattr(st, "st_blocks") else st.st_blocks * 512
    return total


def _dev(path):
    try:
        return os.lstat(path).st_dev
    except OSError:
        return None


def dir_size(p, env):
    path = p.get("path", ".")
    return {"path": path, "bytes": _usage(path, True, p.get("apparent"), _dev(path))}


def disk_usage(p, env):
    root, depth = Path(p.get("path", ".")), int(p.get("depth", 1))
    dev, one_fs, ap = _dev(root), p.get("one_fs", True), p.get("apparent")
    rows = [{"path": str(root), "bytes": _usage(root, one_fs, ap, dev)}]
    level = [root]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                subs = [x for x in d.iterdir() if x.is_dir() and not x.is_symlink() and (not one_fs or _dev(x) == dev)]
            except OSError:
                continue
            for s in subs:
                rows.append({"path": str(s), "bytes": _usage(s, one_fs, ap, dev)})
            nxt += subs
        level = nxt
    rows.sort(key=lambda r: -r["bytes"])
    return rows[: int(p.get("top", 20))]


def disk_free(p, env):
    path = p.get("path") or ("C:\\" if os.name == "nt" else "/")
    u = shutil.disk_usage(path)
    return [{"filesystem": None, "size_bytes": u.total, "used_bytes": u.used, "avail_bytes": u.free,
             "use_pct": round(100 * u.used / u.total) if u.total else 0, "mount": path}]


def _copy_noclobber(src, dst):
    src, dst = Path(src), Path(dst)
    if src.is_dir():
        for d, _, files in os.walk(src):
            rel = Path(d).relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                t = dst / rel / f
                if not t.exists():
                    shutil.copy2(Path(d) / f, t)
    elif not dst.exists():
        shutil.copy2(src, dst)


def copy(p, env):
    srcs, dest = O._srcs(p), Path(p["dest"])
    for s in srcs:
        target = dest / Path(s).name if dest.is_dir() else dest
        _copy_noclobber(s, target)
    return {"copied": len(srcs), "dest": str(dest)}


def _safe_member(name, dest):
    target = (Path(dest) / name).resolve()
    if not str(target).startswith(str(Path(dest).resolve()) + os.sep) and target != Path(dest).resolve():
        raise ValueError(f"archive member escapes destination: {name!r}")
    return target


def archive_list(p, env):
    a = p["archive"]
    if zipfile.is_zipfile(a):
        with zipfile.ZipFile(a) as z:
            return z.namelist()
    with tarfile.open(a) as t:
        return t.getnames()


def archive_extract(p, env):
    a, dest = p["archive"], p["dest"]
    Path(dest).mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(a):
        with zipfile.ZipFile(a) as z:
            names = z.namelist()
            for n in names:
                _safe_member(n, dest)
            z.extractall(dest)
        return {"extracted": len(names), "dest": dest}
    with tarfile.open(a) as t:
        members = t.getmembers()
        for m in members:
            _safe_member(m.name, dest)
            if m.issym() or m.islnk():
                _safe_member(os.path.join(os.path.dirname(m.name), m.linkname), dest)
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
        else:  # pragma: no cover - Python < 3.9.17
            t.extractall(dest)
    return {"extracted": len(members), "dest": dest}


def diff(p, env):
    if p.get("range") is not None:
        raise ValueError("builtin diff compares files only; git range needs git")
    a = Path(p["a"]).read_text(errors="replace", encoding="utf-8").splitlines(keepends=True)
    b = Path(p["b"]).read_text(errors="replace", encoding="utf-8").splitlines(keepends=True)
    return [ln.rstrip("\n") for ln in difflib.unified_diff(a, b, p["a"], p["b"])]


def graveyard_delete(p, env):
    """Move paths into <run>/graveyard; restorable with `rth rollback <run> --apply`."""
    import json
    from .env import state_dir
    yard = Path(p.get("_run_dir") or (state_dir() / "graveyard" / time.strftime("%Y%m%d-%H%M%S"))) / "graveyard"
    yard.mkdir(parents=True, exist_ok=True)
    moved = []
    for i, src in enumerate(O._list(p["paths"])):
        dst = yard / f"{i:04d}-{Path(src).name}"
        shutil.move(src, dst)
        moved.append({"from": os.path.abspath(src), "to": str(dst)})
        (yard / "moves.json").write_text(json.dumps(moved, indent=1), encoding="utf-8")  # after each move: partial runs stay restorable
    return {"moved": moved}


def tasklist_parse(out, p):
    import csv
    import io
    rows = []
    for r in csv.DictReader(io.StringIO(out)):
        mem = re.sub(r"[^\d]", "", r.get("Mem Usage", "") or "")
        rows.append({"pid": int(r.get("PID", 0) or 0), "command": r.get("Image Name"), "rss_kb": int(mem or 0)})
    rows.sort(key=lambda r: -r["rss_kb"])
    if p.get("filter"):
        rows = [r for r in rows if p["filter"].lower() in (r["command"] or "").lower()]
    return O._limit(rows, {"limit": p.get("limit", 15)})


PY = "Pure-Python fallback"
REGISTER = {
    "find-files": O.Impl("builtin", run=find_files, semantics=f"{PY}: does NOT honor .gitignore; sizes are SI like fd (10M = 10^6)."),
    "search-content": O.Impl("builtin", run=search_content, semantics=f"{PY}: Python `re` syntax; does NOT honor .gitignore; skips binary files."),
    "view-file": O.Impl("builtin", run=view_file, semantics=f"{PY}: numbered text."),
    "hex-view": O.Impl("builtin", run=hex_view, semantics=f"{PY}: xxd-like layout."),
    "list-dir": O.Impl("builtin", run=list_dir, semantics=f"{PY}: structured records (name, type, mode, size, mtime)."),
    "tree": O.Impl("builtin", run=tree, semantics=f"{PY}: flat sorted path list; shows ignored files."),
    "disk-usage": O.Impl("builtin", run=disk_usage, semantics=f"{PY}: exact bytes from lstat (st_blocks on Unix); slower than du/dust on large trees."),
    "dir-size": O.Impl("builtin", run=dir_size, semantics=f"{PY}: exact bytes from lstat."),
    "disk-free": O.Impl("builtin", run=disk_free, semantics=f"{PY}: only the filesystem containing `path` (default / or C:\\)."),
    "archive-list": O.Impl("builtin", run=archive_list, semantics=f"{PY}: tarfile/zipfile (tar.gz/bz2/xz, zip; not zst/7z/rar)."),
    "archive-extract": O.Impl("builtin", run=archive_extract, semantics=f"{PY}: rejects members escaping dest (path traversal, unsafe links)."),
    "copy": O.Impl("builtin", run=copy, semantics=f"{PY}: shutil.copy2, never overwrites existing files."),
    "diff": O.Impl("builtin", run=diff, semantics=f"{PY}: difflib unified diff of two files."),
}


def register():
    for name, impl in REGISTER.items():
        if not any(i.tool == "builtin" for i in O.OPS[name].impls):
            O.OPS[name].impls.append(impl)
    pl = O.OPS["process-list"]
    if not any(i.tool == "tasklist" for i in pl.impls):  # Windows
        pl.impls.append(O.Impl("tasklist", lambda p, env: ["tasklist", "/fo", "csv"], tasklist_parse,
                               semantics="Windows tasklist: memory only, no CPU%."))
    sd = O.OPS["safe-delete"]
    if not any(i.tool == "builtin" for i in sd.impls):
        sd.impls.append(O.Impl("builtin", run=graveyard_delete,
                               semantics="Harness graveyard in the run directory (not the desktop trash); restore with `rth rollback <run> --apply`."))


register()


# ----------------------------------------------------------------------------- jq subset

class JqUnsupported(ValueError):
    pass


_CMP = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def _split_top(expr, sep):
    depth, cur, out, q = 0, "", [], False
    for ch in expr:
        if ch == '"':
            q = not q
        elif not q and ch in "([":
            depth += 1
        elif not q and ch in ")]":
            depth -= 1
        if ch == sep and depth == 0 and not q:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _path(expr, values):
    """.a.b, ."key", .[0], .[], .a[] .a[1] — applied to a stream of values."""
    i, vals = 1, values
    expr = expr.strip()
    if expr == ".":
        return vals
    if not expr.startswith("."):
        raise JqUnsupported(expr)
    while i < len(expr):
        if expr[i] == "[":
            j = expr.index("]", i)
            inner = expr[i + 1:j].strip()
            if inner == "":
                vals = [x for v in vals for x in (v.values() if isinstance(v, dict) else v)]
            elif re.fullmatch(r"-?\d+", inner):
                vals = [v[int(inner)] if isinstance(v, list) and -len(v) <= int(inner) < len(v) else None for v in vals]
            elif re.fullmatch(r'"[^"]*"', inner):
                vals = [v.get(inner[1:-1]) if isinstance(v, dict) else None for v in vals]
            else:
                raise JqUnsupported(expr)
            i = j + 1
        else:
            if expr[i] == ".":
                i += 1
                if i < len(expr) and expr[i] == "[":
                    continue
            m = re.match(r'"([^"]*)"|([A-Za-z_][\w]*)', expr[i:])
            if not m:
                raise JqUnsupported(expr)
            key = m[1] if m[1] is not None else m[2]
            vals = [v.get(key) if isinstance(v, dict) else None for v in vals]
            i += m.end()
    return vals


def _literal(s):
    import json as _json
    try:
        return _json.loads(s)
    except ValueError:
        raise JqUnsupported(s)


def _cond(expr, v):
    """select() condition: A and B / A or B / not / path OP literal / path OP path / test("re") / has("k")."""
    expr = expr.strip()
    for op_, fn in ((" or ", any), (" and ", all)):
        parts = _split_words(expr, op_)
        if len(parts) > 1:
            return fn(_cond(p, v) for p in parts)
    if expr.endswith("| not"):
        return not _cond(expr[: -len("| not")], v)
    m = re.fullmatch(r'(\.[^|]*?)\s*\|\s*test\("((?:[^"\\]|\\.)*)"\)', expr) or re.fullmatch(r'(\.\S*?)\s*\|\s*test\("(.*)"\)', expr)
    if m:
        x = _path(m[1], [v])[0]
        return isinstance(x, str) and re.search(m[2], x) is not None
    m = re.fullmatch(r'has\("([^"]*)"\)', expr)
    if m:
        return isinstance(v, dict) and m[1] in v
    cm = re.fullmatch(r"\s*(\.[^=!<>]*?)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*", expr)
    if cm:
        left = _path(cm[1], [v])[0]
        right = _path(cm[3], [v])[0] if cm[3].startswith(".") else _literal(cm[3])
        try:
            return _CMP[cm[2]](left, right)
        except TypeError:
            return False
    if expr.startswith("."):
        x = _path(expr, [v])[0]
        return x not in (None, False)
    raise JqUnsupported(expr)


def _split_words(expr, sep):
    out, depth, cur, i, q = [], 0, "", 0, False
    while i < len(expr):
        ch = expr[i]
        if ch == '"':
            q = not q
        if not q and ch in "([":
            depth += 1
        elif not q and ch in ")]":
            depth -= 1
        if not q and depth == 0 and expr.startswith(sep, i):
            out.append(cur)
            cur, i = "", i + len(sep)
            continue
        cur += ch
        i += 1
    return out + [cur]


def _key(path):
    def k(v):
        x = _path(path, [v])[0]
        return (x is None, str(type(x)), x if not isinstance(x, (dict, list)) else str(x))
    return k


SIMPLE = {
    "sort": lambda v: sorted(v, key=lambda x: (str(type(x)), x if not isinstance(x, (dict, list)) else str(x))),
    "unique": lambda v: sorted({json_key(x): x for x in v}.values(), key=lambda x: json_key(x)),
    "reverse": lambda v: list(reversed(v)) if isinstance(v, list) else v[::-1],
    "add": lambda v: (sum(v) if all(isinstance(x, (int, float)) for x in v) else "".join(v) if all(isinstance(x, str) for x in v)
                      else [y for x in v for y in x] if all(isinstance(x, list) for x in v) else {k: w for x in v for k, w in x.items()}) if v else None,
    "min": lambda v: min(v) if v else None,
    "max": lambda v: max(v) if v else None,
    "first": lambda v: v[0] if v else None,
    "last": lambda v: v[-1] if v else None,
    "to_entries": lambda v: [{"key": k, "value": w} for k, w in v.items()],
    "from_entries": lambda v: {e.get("key", e.get("name")): e.get("value") for e in v},
    "keys_unsorted": lambda v: list(v.keys()),
    "values": lambda v: list(v.values()) if isinstance(v, dict) else v,
    "tostring": lambda v: v if isinstance(v, str) else __import__("json").dumps(v),
    "tonumber": lambda v: float(v) if "." in str(v) else int(v),
    "ascii_downcase": lambda v: v.lower(),
    "ascii_upcase": lambda v: v.upper(),
    "type": lambda v: {dict: "object", list: "array", str: "string", bool: "boolean", type(None): "null"}.get(type(v), "number"),
    "not": lambda v: v in (None, False),
}


def json_key(x):
    import json as _json
    return _json.dumps(x, sort_keys=True)


def _term(t, vals):
    t = t.strip()
    if t in SIMPLE:
        return [SIMPLE[t](v) for v in vals]
    m = re.fullmatch(r"(sort_by|group_by|unique_by|min_by|max_by)\((\..*)\)", t, re.S)
    if m:
        fn, k = m[1], _key(m[2])
        out = []
        for v in vals:
            if fn == "sort_by":
                out.append(sorted(v, key=k))
            elif fn == "group_by":
                groups = {}
                for x in sorted(v, key=k):
                    groups.setdefault(json_key(_path(m[2], [x])[0]), []).append(x)
                out.append(list(groups.values()))
            elif fn == "unique_by":
                seen = {}
                for x in sorted(v, key=k):
                    seen.setdefault(json_key(_path(m[2], [x])[0]), x)
                out.append(list(seen.values()))
            else:
                out.append((min if fn == "min_by" else max)(v, key=k) if v else None)
        return out
    m = re.fullmatch(r'join\("((?:[^"\\]|\\.)*)"\)', t)
    if m:
        return [m[1].join("" if x is None else str(x) for x in v) for v in vals]
    m = re.fullmatch(r'split\("((?:[^"\\]|\\.)*)"\)', t)
    if m:
        return [v.split(m[1]) for v in vals]
    m = re.fullmatch(r'has\("([^"]*)"\)', t)
    if m:
        return [isinstance(v, dict) and m[1] in v for v in vals]
    m = re.fullmatch(r'test\("((?:[^"\\]|\\.)*)"\)', t)
    if m:
        return [isinstance(v, str) and re.search(m[1], v) is not None for v in vals]
    m = re.fullmatch(r"\{(.*)\}", t, re.S)
    if m:  # object construction: {a, b: .x, "c": .y}
        out = []
        for v in vals:
            obj = {}
            for part in _split_top(m[1], ","):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    k, e = part.split(":", 1)
                    obj[k.strip().strip('"')] = jq_lite(e.strip(), [v])[0] if jq_lite(e.strip(), [v]) else None
                else:
                    obj[part.strip('"')] = v.get(part.strip('"')) if isinstance(v, dict) else None
            out.append(obj)
        return out
    if re.fullmatch(r'-?\d+(\.\d+)?|"[^"]*"|true|false|null', t):
        return [_literal(t) for _ in vals]
    if t == "length":
        return [len(v) if v is not None else 0 for v in vals]
    if t == "keys":
        return [sorted(v.keys()) if isinstance(v, dict) else list(range(len(v))) for v in vals]
    m = re.fullmatch(r"map\((.*)\)", t, re.S)
    if m:
        return [[x for item in (v or []) for x in jq_lite(m[1], [item])] for v in vals]
    m = re.fullmatch(r"select\((.*)\)", t, re.S)
    if m:
        return [v for v in vals if _cond(m[1], v)]
    m = re.fullmatch(r"\[(.*)\]", t, re.S)
    if m and not t.startswith(".["):
        return [jq_lite(m[1], [v]) for v in vals]
    if t.startswith("."):
        return _path(t, vals)
    raise JqUnsupported(t)


def jq_lite(expr, vals):
    for part in _split_top(expr, "|"):
        vals = _term(part, vals)
    return vals


def json_query(p, env):
    import json as _json
    data = _json.loads(Path(p["file"]).read_text(encoding="utf-8") if p.get("file") else p["stdin"])
    try:
        out = jq_lite(p.get("filter", "."), [data])
    except JqUnsupported as e:
        raise ValueError(f"filter outside the builtin jq subset ({e}); install jaq or jq")
    return out[0] if len(out) == 1 else out


def _jq_supported(p, env):
    try:
        jq_lite(p.get("filter", "."), [{}])
        return None
    except JqUnsupported as e:
        return f"filter uses jq features outside the builtin subset ({e})"
    except (TypeError, AttributeError, KeyError, IndexError):
        return None  # syntax is supported; data-dependent errors surface at run time


_jq = O.OPS["json-query"]
_jq.impls = [i for i in _jq.impls if i.tool != "builtin"] + [
    O.Impl("builtin", run=json_query, when=_jq_supported,
           semantics="Builtin jq subset: paths, .[], |, [...], {...}, length, keys, map, select (and/or/not/test/has), "
                     "sort(_by), group_by, unique(_by), min/max(_by), add, first, last, reverse, join, split, to/from_entries, "
                     "type, tostring/tonumber, literals. Anything else is refused.")]
