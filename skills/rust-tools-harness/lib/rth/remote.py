"""Remote targets: run read-only ops on another host (ssh:HOST) or container (docker:NAME / podman:NAME).

Scope, deliberately narrow:
  * the remote environment is detected on the remote side (which binaries exist there), cached per target;
  * external-tool implementations run through ssh / docker exec; pure-Python builtins run locally only, so
    they are skipped for remote steps rather than silently answering about the local machine;
  * writes, history rewrites and ops that leave local artifacts are refused remotely: there is no undo,
    lock or verification of the far side.
"""
import hashlib
import json
import shlex
import shutil
import subprocess
import time

from . import registry
from .env import NATIVE_BINARIES, state_dir

REMOTE_UNSUPPORTED_OPS = {"benchmark", "cpu-profile", "python-profile", "syscall-trace", "net-bandwidth", "task-list"}
TTL = 3600


class RemoteError(RuntimeError):
    pass


def parse(target):
    kind, _, name = target.partition(":")
    if kind not in ("ssh", "docker", "podman") or not name:
        raise ValueError(f"target must be ssh:HOST, docker:CONTAINER or podman:CONTAINER, got {target!r}")
    return kind, name


def wrap(target, argv):
    kind, name = parse(target)
    if kind == "ssh":
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", name, "--", shlex.join([str(a) for a in argv])]
    return [kind, "exec", "-i", name, *[str(a) for a in argv]]


class RemoteEnv:
    """Duck-types env.Env for the dispatcher: has/which/identity/help_text/supports_flag/version on the remote."""
    remote = True

    def __init__(self, target, data):
        self.target, self.data = target, data

    @property
    def os(self):
        return self.data["os"]

    @property
    def is_gnu(self):
        return self.data["os"] == "linux"

    def _run(self, argv, timeout=15):
        r = subprocess.run(wrap(self.target, argv), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr).strip()

    def which(self, binary):
        return self.data["binaries"].get(binary)

    def has(self, binary):
        return binary in self.data["binaries"]

    def version(self, binary, args=("--version",)):
        v = self.data.setdefault("versions", {})
        if binary not in v and self.has(binary):
            code, out = self._run([binary, *args])
            v[binary] = {"exit": code, "text": out.splitlines()[0][:160] if out else ""}
        return v.get(binary)

    def identity(self, tool):
        for b in filter(None, [tool["binary"], *tool["aliases"]]):
            if self.has(b):
                ver = (self.version(b) or {}).get("text", "").lower()
                state = "confirmed" if any(t in ver for t in registry.identity_tokens(tool)) else "unconfirmed"
                return {"state": state, "binary": b, "version": ver}
        return {"state": "missing"}

    def help_text(self, binary):
        h = self.data.setdefault("help", {})
        if binary not in h and self.has(binary):
            h[binary] = self._run([binary, "--help"])[1][:20000]
        return h.get(binary, "")

    def supports_flag(self, binary, flag):
        return flag in self.help_text(binary)

    def installed_tool_ids(self):
        return {i for b in self.data["binaries"] for i in registry.binaries().get(b, [])}

    def summary(self):
        return {k: self.data[k] for k in ("target", "os", "arch", "uid")} | {"binaries_found": sorted(self.data["binaries"])}


def detect(target, refresh=False):
    kind, name = parse(target)
    if not shutil.which(kind):
        raise RemoteError(f"{kind} is not installed locally")
    cache = state_dir() / f"env-remote-{hashlib.sha1(target.encode()).hexdigest()[:12]}.json"
    if not refresh and cache.exists():
        d = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - d.get("detected_at", 0) < TTL:
            return RemoteEnv(target, d)
    names = sorted(set(registry.binaries()) | set(NATIVE_BINARIES))
    script = ('uname -s; uname -m; id -u; for b in ' + " ".join(shlex.quote(n) for n in names) +
              '; do p=$(command -v "$b" 2>/dev/null) && printf "%s\\t%s\\n" "$b" "$p"; done')
    r = subprocess.run(wrap(target, ["sh", "-c", script]), capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and not r.stdout:
        raise RemoteError(f"cannot reach {target}: {r.stderr.strip()[:300]}")
    lines = r.stdout.splitlines()
    data = {"target": target, "detected_at": time.time(), "os": lines[0].lower() if lines else "unknown",
            "arch": lines[1] if len(lines) > 1 else None, "uid": lines[2] if len(lines) > 2 else None,
            "binaries": dict(ln.split("\t", 1) for ln in lines[3:] if "\t" in ln), "privileges": {"is_root": len(lines) > 2 and lines[2] == "0"},
            "path_hash": "remote:" + target, "tty": False, "versions": {}, "help": {}, "package_managers": []}
    cache.write_text(json.dumps(data), encoding="utf-8")
    return RemoteEnv(target, data)
