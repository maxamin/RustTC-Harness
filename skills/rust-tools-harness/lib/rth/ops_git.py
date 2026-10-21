"""Git write ops (gated), with undo that never discards work.

git-commit / git-branch: risk writes-files. git-absorb: risk rewrites-history (its own --allow history gate).
Each records HEAD/branches before and after; rollback uses only non-destructive git: `reset --soft` (commit:
changes return to the index), `reset --keep` (absorb: refuses if it would lose local changes), `branch -d`
(safe delete; refuses unmerged work).
"""
import subprocess

from .ops import Impl, Op, op, flag, WRITES

HISTORY = "rewrites-history"


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def _repo(p):
    return p.get("path", ".")


def _commit(p, env):
    repo = _repo(p)
    files = p.get("files") or []
    files = [files] if isinstance(files, str) else files
    if files:
        _git(repo, "add", "--", *files)
    staged = _git(repo, "diff", "--cached", "--name-only")
    if not staged:
        raise ValueError("nothing staged to commit (pass files, or stage changes first)")
    _git(repo, "commit", "-m", p["message"], *flag(p.get("no_verify"), "--no-verify"))
    return {"commit": _git(repo, "rev-parse", "--short", "HEAD"), "files": staged.splitlines(),
            "subject": _git(repo, "log", "-1", "--format=%s")}


def _branch(p, env):
    repo = _repo(p)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{p['name']}", check=False):
        raise ValueError(f"branch {p['name']} already exists")
    if p.get("switch"):
        _git(repo, "switch", "-c", p["name"], *([p["start"]] if p.get("start") else []))
    else:
        _git(repo, "branch", p["name"], *([p["start"]] if p.get("start") else []))
    return {"branch": p["name"], "at": _git(repo, "rev-parse", "--short", p["name"]), "switched": bool(p.get("switch"))}


def _absorb(p, env):
    repo = _repo(p)
    args = ["absorb", *flag(p.get("and_rebase"), "--and-rebase"), *flag(p.get("base"), "--base", str(p.get("base")))]
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500])
    return {"output": (r.stdout + r.stderr).strip().splitlines()[-30:], "head": _git(repo, "rev-parse", "--short", "HEAD")}


def _git_undo(kind):
    return lambda p: {"git": {"repo": _repo(p), "kind": kind, **({"branch": p["name"]} if kind == "branch" else {})}}


op(Op("git-commit", "vcs", "Commit staged changes (or the given files) with a message",
      {"path": "repo (.)", "message": "REQUIRED commit message", "files": "paths to stage first (optional)", "no_verify": "bool: skip hooks"},
      [Impl("builtin", run=_commit, requires=("git",), semantics="git add -- files; git commit -m. Undo: git reset --soft (changes stay staged).")],
      risk=WRITES, verify={"not_empty": True}, undo=_git_undo("commit")))

op(Op("git-branch", "vcs", "Create a branch (optionally switch to it; uncommitted changes are carried over)",
      {"path": "repo (.)", "name": "REQUIRED branch name", "start": "start point (HEAD)", "switch": "bool"},
      [Impl("builtin", run=_branch, requires=("git",), semantics="git branch / git switch -c. Undo: switch back + git branch -d (safe).")],
      risk=WRITES, verify={"not_empty": True}, undo=_git_undo("branch")))

op(Op("git-absorb-preview", "git-fixup", "Show which earlier commits staged hunks would be absorbed into (no changes)",
      {"path": "repo (.)", "base": "limit search to commits after this ref"},
      [Impl("git-absorb", lambda p, env: ["git", "-C", _repo(p), "absorb", "--dry-run", *flag(p.get("base"), "--base", str(p.get("base")))],
            lambda o, p: o.strip().splitlines(), registry_id="git-absorb", requires=("git",),
            when=lambda p, env: None if env.has("git-absorb") else "git-absorb not installed")],
      verify={"exit_in": [0]}))

op(Op("git-absorb", "git-fixup", "Create fixup! commits for staged hunks; with and_rebase also autosquash (REWRITES HISTORY)",
      {"path": "repo (.)", "and_rebase": "bool: rewrite history now", "base": "limit search"},
      [Impl("builtin", run=_absorb, requires=("git", "git-absorb"), registry_id="git-absorb",
            semantics="git absorb. Undo: git reset --keep to the recorded HEAD (refuses if it would lose local changes).")],
      risk=HISTORY, verify={"not_empty": True}, undo=_git_undo("absorb")))
