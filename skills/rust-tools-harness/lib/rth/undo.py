"""Undo for write steps: snapshot before, fingerprint after, roll back on request.

Each write op declares what it will touch (Op.undo -> {"modify": [...], "create": [...]}).
Before the step runs, files to be modified are copied into <run>/backup/<step>/; paths to be
created are recorded with whether they already existed. After the step, post-write hashes
are stored so rollback can refuse to clobber later edits (unless forced).

Rollback never deletes: created paths are moved into <run>/rolled-back/, so a rollback is
itself reversible.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def _sha(path):
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


RESTORE_HINTS = {"trash": "trash restore  (trashy)", "rip": "rip -u  (restores the last burial)",
                 "gio": "gio trash --list, then restore from your file manager's Trash",
                 "trash-put": "trash-restore  (trash-cli, interactive)"}


def _tree_fp(path):
    """Fingerprint of a directory tree (relative paths, sizes, mtimes) to detect later edits."""
    if not os.path.isdir(path):
        return _sha(path)
    h = hashlib.sha256()
    for d, dirs, files in os.walk(path):
        dirs.sort()
        for f in sorted(files):
            fp = os.path.join(d, f)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            h.update(f"{os.path.relpath(fp, path)}\0{st.st_size}\0{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def _gio_uri(original):
    """`gio trash --list` lines are 'trash:///NAME<TAB>/original/path'; newest entry wins."""
    r = subprocess.run(["gio", "trash", "--list"], capture_output=True, text=True)
    hits = [ln.split("\t", 1)[0] for ln in r.stdout.splitlines() if ln.split("\t", 1)[-1] == original]
    return hits[-1] if hits else None


def _git_q(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_do(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:300]


def snapshot(run_dir, step_id, spec):
    bdir = Path(run_dir) / "backup" / step_id
    bdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, p in enumerate(spec.get("modify", [])):
        ap = os.path.abspath(p)
        if os.path.isfile(ap):
            dst = bdir / f"{i:04d}-{Path(ap).name}"
            shutil.copy2(ap, dst)
            entries.append({"path": ap, "action": "restore", "backup": str(dst), "sha_before": _sha(ap)})
        else:
            entries.append({"path": ap, "action": "none", "note": "not a regular file before the step; not backed up"})
    for p in spec.get("create", []):
        ap = os.path.abspath(p)
        existed = os.path.lexists(ap)
        entry = {"path": ap, "action": "remove-created", "existed_before": existed}
        if existed and os.path.isdir(ap):
            entry["children_before"] = sorted(os.listdir(ap))
        entries.append(entry)
    if spec.get("git"):
        g = spec["git"]
        repo = os.path.abspath(g["repo"])
        entries.append({"path": repo, "action": "git", "kind": g["kind"], "branch_target": g.get("branch"),
                        "head_before": _git_q(repo, "rev-parse", "HEAD"), "branch_before": _git_q(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                        "branches_before": (_git_q(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads") or "").splitlines()})
    for p in spec.get("deleted", []):
        entries.append({"path": os.path.abspath(p), "action": "restore-deleted"})
    manifest = {"step": step_id, "entries": entries, "finalized": False, "run_dir": str(run_dir)}
    (bdir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return str(bdir / "manifest.json")


def finalize(manifest_path, tool=None):
    m = json.loads(Path(manifest_path).read_text())
    m["tool"] = tool
    for e in m["entries"]:
        if e["action"] in ("restore", "remove-created"):
            e["sha_after"] = _tree_fp(e["path"]) if e["action"] == "remove-created" else _sha(e["path"])
            e["exists_after"] = os.path.lexists(e["path"])
    for e in m["entries"]:
        if e["action"] == "git":
            e["head_after"] = _git_q(e["path"], "rev-parse", "HEAD")
            e["branches_after"] = (_git_q(e["path"], "for-each-ref", "--format=%(refname:short)", "refs/heads") or "").splitlines()
    moves = Path(m.get("run_dir", "")) / "graveyard" / "moves.json"
    if moves.exists():
        where = {mv["from"]: mv["to"] for mv in json.loads(moves.read_text())}
        for e in m["entries"]:
            if e["action"] == "restore-deleted" and e["path"] in where:
                e["graveyard"] = where[e["path"]]
    m["finalized"] = True
    Path(manifest_path).write_text(json.dumps(m, indent=1))
    restore = [e for e in m["entries"] if e["action"] == "restore"]
    created = [e for e in m["entries"] if e["action"] == "remove-created"]
    return {"modified": sum(e["sha_after"] != e["sha_before"] for e in restore), "modify_expected": len(restore),
            "created": sum(1 for e in created if e["exists_after"] and not e["existed_before"]), "create_expected": len(created)}


def _rollback_git(e, apply, force):
    repo, kind = e["path"], e["kind"]
    head_now = _git_q(repo, "rev-parse", "HEAD")
    if kind in ("commit", "absorb"):
        if head_now == e.get("head_before"):
            return {"action": "none", "why": "HEAD already at the pre-run commit"}
        if head_now != e.get("head_after") and not force:
            return {"action": "conflict", "why": "HEAD moved after the run (new commits); rerun with force"}
        if kind == "commit":
            cmd = ["reset", "--soft", e["head_before"]] if e.get("head_before") else ["update-ref", "-d", "HEAD"]
        else:
            cmd = ["reset", "--keep", e["head_before"]]
        out = {"action": "git " + " ".join(cmd[:2]), "to": e.get("head_before")}
        if apply:
            ok, msg = _git_do(repo, *cmd)
            if not ok:
                out.update(action="failed", why=msg)
        return out
    if kind == "branch":
        new = sorted(set(e.get("branches_after", [])) - set(e.get("branches_before", [])))
        if not new:
            return {"action": "none", "why": "no branch was created"}
        out = {"action": "git branch -d", "branches": new}
        if apply:
            cur = _git_q(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if cur in new and e.get("branch_before"):
                ok, msg = _git_do(repo, "switch", e["branch_before"])
                if not ok:
                    return {"action": "failed", "why": f"could not switch back to {e['branch_before']}: {msg}"}
            for b in new:
                ok, msg = _git_do(repo, "branch", "-d", b)
                if not ok:
                    out.update(action="failed", why=msg)
        return out
    return {"action": "none", "why": f"unknown git undo kind {kind}"}


def _aside(run_dir, path, n):
    dst_dir = Path(run_dir) / "rolled-back"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"{n:04d}-{Path(path).name}"
    shutil.move(path, dst)
    return str(dst)


def rollback(run_dir, *, apply=False, force=False):
    """Restore modified files and move created paths aside. Dry run unless apply=True.

    All-or-nothing: if any path conflicts (changed after the run) nothing is applied unless force.
    """
    preview = _rollback(Path(run_dir), apply=False, force=force)
    preview["conflicts"] = sum(1 for a in preview["actions"] if a["action"] == "conflict")
    if not apply:
        return preview
    if preview.get("conflicts") and not force:
        return {**preview, "note": "not applied: resolve conflicts or rerun with force (all-or-nothing)"}
    return _rollback(Path(run_dir), apply=True, force=force)


def _rollback(run_dir, *, apply, force):
    manifests = sorted(run_dir.glob("backup/*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    report, n = [], 0
    if not manifests:
        return {"run_dir": str(run_dir), "applied": False, "actions": [], "note": "no write steps with undo data in this run"}
    for mp in manifests:
        m = json.loads(mp.read_text())
        if not m.get("finalized"):
            report.append({"step": m["step"], "action": "skip", "why": "step did not complete; nothing recorded as written"})
            continue
        for e in m["entries"]:
            act = {"step": m["step"], "path": e["path"]}
            if e["action"] == "restore":
                cur = _sha(e["path"])
                if cur == e.get("sha_before"):
                    act.update(action="none", why="already identical to the backup")
                elif cur != e.get("sha_after") and not force:
                    act.update(action="conflict", why="file changed after the step; rerun with force to overwrite")
                else:
                    act.update(action="restore")
                    if apply:
                        shutil.copy2(e["backup"], e["path"])
            elif e["action"] == "git":
                act.update(_rollback_git(e, apply, force))
            elif e["action"] == "restore-deleted":
                if e.get("graveyard"):
                    if os.path.lexists(e["path"]) and not force:
                        act.update(action="conflict", why="original path exists again; rerun with force")
                    elif not os.path.lexists(e["graveyard"]):
                        act.update(action="none", why="no longer in the graveyard")
                    else:
                        act.update(action="restore-from-graveyard")
                        if apply:
                            os.makedirs(os.path.dirname(e["path"]), exist_ok=True)
                            shutil.move(e["graveyard"], e["path"])
                elif m.get("tool") == "gio" and shutil.which("gio"):
                    uri = _gio_uri(e["path"])
                    if not uri:
                        act.update(action="none", why="not found in the trash (emptied or restored already)")
                    elif os.path.lexists(e["path"]) and not force:
                        act.update(action="conflict", why="original path exists again; rerun with force")
                    else:
                        act.update(action="restore-from-trash", trash_uri=uri)
                        if apply:
                            r = subprocess.run(["gio", "trash", "--restore", *(["--force"] if force else []), uri],
                                               capture_output=True, text=True)
                            if r.returncode != 0:
                                act.update(action="failed", why=r.stderr.strip()[:300])
                else:
                    act.update(action="manual", why="moved to the system trash by " + str(m.get("tool")) + "; restore with: "
                               + RESTORE_HINTS.get(m.get("tool"), "your trash tool"))
            elif e["action"] == "remove-created":
                if not os.path.lexists(e["path"]):
                    act.update(action="none", why="no longer exists")
                elif e["existed_before"] and os.path.isdir(e["path"]):
                    new = sorted(set(os.listdir(e["path"])) - set(e.get("children_before", [])))
                    act.update(action="move-aside", children=new)
                    if apply:
                        for c in new:
                            n += 1
                            _aside(run_dir, os.path.join(e["path"], c), n)
                elif e["existed_before"]:
                    act.update(action="none", why="existed before the step (copy/extract never overwrite)")
                elif _tree_fp(e["path"]) != e.get("sha_after") and not force:
                    act.update(action="conflict", why="created path changed after the step; rerun with force")
                else:
                    act.update(action="move-aside")
                    if apply:
                        n += 1
                        act["moved_to"] = _aside(run_dir, e["path"], n)
            else:
                act.update(action="none", why=e.get("note"))
            report.append(act)
    conflicts = [a for a in report if a["action"] == "conflict"]
    return {"run_dir": str(run_dir), "applied": apply, "actions": report, "conflicts": len(conflicts),
            "note": None if apply else "dry run: rerun with apply to perform these actions"}
