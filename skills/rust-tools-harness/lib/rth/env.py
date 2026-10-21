"""Environment detector: OS, shell, arch, privileges, runtimes, installed binaries.

Results are cached in the state dir for ENV_TTL seconds and invalidated when
PATH changes. Version probes run lazily (only for binaries that exist) and
confirm identity, so `hx` (helix vs hex) or `trash` (trashy vs trash-cli)
collisions are reported instead of silently trusted.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import registry

ENV_TTL = 3600

# Non-catalogue commands the fallbacks and adapters rely on.
NATIVE_BINARIES = [
    "sh", "bash", "nproc", "wc", "sort", "cut", "find", "grep", "git", "sed", "awk", "cat", "head", "tail", "ls", "tree", "du", "df", "ps", "pgrep",
    "top", "free", "vmstat", "uptime", "uname", "lscpu", "hostnamectl", "sw_vers", "sysctl",
    "jq", "python3", "perl", "xxd", "od", "file", "diff", "cmp", "tar", "unzip", "zip", "7z", "gzip", "zstd",
    "cp", "rsync", "gio", "trash-put", "kill", "ss", "netstat", "lsof", "ip", "mtr", "traceroute", "tracepath",
    "nmap", "nc", "tcpdump", "nethogs", "iftop", "strace", "perf", "dtrace", "make", "npm", "cloc", "man",
    "fzf", "tldr", "timeout", "sudo", "curl", "wget", "sqlite3", "bpftool",
]
RUNTIMES = {"python3": ["--version"], "node": ["--version"], "cargo": ["--version"], "rustc": ["--version"],
            "go": ["version"], "docker": ["--version"], "perl": ["-e", "print $]"]}
PACKAGE_MANAGERS = ["apt", "dnf", "yum", "pacman", "zypper", "apk", "brew", "port", "nix", "winget", "scoop",
                    "choco", "cargo", "cargo-binstall", "pip", "pipx", "npm", "mise"]


def state_dir():
    d = Path(os.environ.get("RTH_STATE") or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "rust-tools-harness")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(argv, timeout=3):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _os_release():
    txt = _read("/etc/os-release") or ""
    kv = dict(line.split("=", 1) for line in txt.splitlines() if "=" in line)
    return {k: v.strip('"') for k, v in kv.items() if k in ("ID", "ID_LIKE", "VERSION_ID", "PRETTY_NAME")}


def _privileges():
    p = {"is_root": hasattr(os, "geteuid") and os.geteuid() == 0}
    if sys.platform.startswith("linux"):
        p["perf_event_paranoid"] = _read("/proc/sys/kernel/perf_event_paranoid")
        p["ptrace_scope"] = _read("/proc/sys/kernel/yama/ptrace_scope")
        status = _read("/proc/self/status") or ""
        eff = next((ln.split()[1] for ln in status.splitlines() if ln.startswith("CapEff:")), None)
        p["cap_eff"] = eff
        p["unprivileged_icmp"] = _read("/proc/sys/net/ipv4/ping_group_range")
    p["sudo_available"] = shutil.which("sudo") is not None
    return p


def _path_hash():
    return hashlib.sha1(os.environ.get("PATH", "").encode()).hexdigest()[:12]


def detect(refresh=False):
    cache = state_dir() / "env.json"
    if not refresh and cache.exists():
        try:
            env = json.loads(cache.read_text(encoding="utf-8"))
            if env.get("path_hash") == _path_hash() and time.time() - env.get("detected_at", 0) < ENV_TTL:
                return Env(env)
        except (ValueError, OSError):
            pass
    found = {}
    for b, ids in registry.binaries().items():
        path = shutil.which(b)
        if path:
            found[b] = {"path": path, "claimed_by": ids}
    natives = {b: shutil.which(b) for b in NATIVE_BINARIES if shutil.which(b)}
    runtimes = {}
    for r, args in RUNTIMES.items():
        if shutil.which(r):
            code, out = _run([r, *args])
            runtimes[r] = out.splitlines()[0] if out else None
    env = {
        "detected_at": time.time(),
        "path_hash": _path_hash(),
        "os": platform.system().lower(),
        "os_release": _os_release() if sys.platform.startswith("linux") else {},
        "kernel": platform.release(),
        "arch": platform.machine(),
        "shell": os.environ.get("SHELL"),
        "tty": sys.stdin.isatty() and sys.stdout.isatty(),
        "ci": bool(os.environ.get("CI")),
        "cwd_in_git": _run(["git", "rev-parse", "--is-inside-work-tree"])[1] == "true" if shutil.which("git") else False,
        "privileges": _privileges(),
        "runtimes": runtimes,
        "package_managers": [m for m in PACKAGE_MANAGERS if shutil.which(m)],
        "binaries": found,
        "natives": natives,
        "versions": {},
    }
    e = Env(env)
    e.save()
    return e


class Env:
    def __init__(self, data):
        self.data = data

    def save(self):
        (state_dir() / "env.json").write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    @property
    def os(self):
        return self.data["os"]

    @property
    def is_gnu(self):
        return self.os == "linux" and "busybox" not in (self.data.get("natives", {}).get("find") or "")

    def which(self, binary):
        b = self.data["binaries"].get(binary)
        if b:
            return b["path"]
        if binary in self.data["natives"]:
            return self.data["natives"][binary]
        return shutil.which(binary)  # not in our lists: ask the OS, don't guess

    def has(self, binary):
        return self.which(binary) is not None

    def version(self, binary, args=("--version",)):
        """Probe `binary --version` once; cached in env.json."""
        v = self.data.setdefault("versions", {})
        if binary not in v:
            path = self.which(binary)
            if not path:
                return None
            code, out = _run([path, *args])
            v[binary] = {"exit": code, "text": (out or "").splitlines()[0][:160] if out else ""}
            self.save()
        return v[binary]

    def identity(self, tool):
        """Is the binary on PATH really this registry tool? confirmed | unconfirmed | conflict | missing."""
        for b in filter(None, [tool["binary"], *tool["aliases"]]):
            if not self.has(b):
                continue
            ver = (self.version(b) or {}).get("text", "").lower()
            if any(tok in ver for tok in registry.identity_tokens(tool)):
                return {"state": "confirmed", "binary": b, "version": ver}
            others = [registry.tools()[i] for i in registry.binaries().get(b, []) if i != tool["id"]]
            for o in others:
                if any(tok in ver for tok in registry.identity_tokens(o)):
                    return {"state": "conflict", "binary": b, "version": ver, "actually": o["id"]}
            return {"state": "unconfirmed", "binary": b, "version": ver}
        return {"state": "missing"}

    def help_text(self, binary):
        """Cached `--help` output for flag-support checks."""
        h = self.data.setdefault("help", {})
        if binary not in h:
            path = self.which(binary)
            if not path:
                return ""
            _, out = _run([path, "--help"], timeout=5)
            h[binary] = out[:20000]
            self.save()
        return h[binary]

    def supports_flag(self, binary, flag):
        return flag in self.help_text(binary)

    def installed_tool_ids(self):
        """Catalogue tools whose binary is on PATH and not identified as a different tool."""
        tools = registry.tools()
        ids = set()
        for info in self.data["binaries"].values():
            for tid in info["claimed_by"]:
                if self.identity(tools[tid])["state"] in ("confirmed", "unconfirmed"):
                    ids.add(tid)
        return ids

    def summary(self):
        d = self.data
        return {
            "os": d["os"], "distro": d["os_release"].get("PRETTY_NAME"), "kernel": d["kernel"], "arch": d["arch"],
            "shell": d["shell"], "tty": d["tty"], "ci": d["ci"], "cwd_in_git": d["cwd_in_git"],
            "privileges": d["privileges"], "runtimes": d["runtimes"], "package_managers": d["package_managers"],
            "catalogue_binaries_found": sorted(d["binaries"]),
            "native_binaries_found": sorted(d["natives"]),
        }
