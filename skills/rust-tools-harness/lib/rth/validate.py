"""Validator: evidence checks on step results. A zero exit code alone never counts as success."""
import os
import re

RECORD_KEYS = ("matches", "results", "files", "changes", "hops", "languages", "by_extension", "fields", "changed")


def records(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in RECORD_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
    return None


def check(spec, *, exit_code, data, stdout, inputs, parsed):
    """Run the checks in `spec`; returns {"passed", "evidence", "checks"}.

    `evidence` is True only if at least one check beyond the exit code passed.
    """
    out = []

    def add(name, ok, detail=None):
        out.append({"check": name, "passed": bool(ok), **({"detail": detail} if detail is not None else {})})

    if "exit_in" in spec:
        add("exit_in", exit_code in spec["exit_in"] or exit_code is None and parsed, f"exit={exit_code}")
    if parsed is not None:
        add("output_parsed", parsed, None if parsed else "normalizer could not parse output")
    if spec.get("not_empty"):
        add("not_empty", bool(data) or bool((stdout or "").strip()))
    recs = records(data)
    if "min_records" in spec:
        n = len(recs) if recs is not None else (1 if data else 0)
        add("min_records", n >= spec["min_records"], f"{n} >= {spec['min_records']}")
    if "max_records" in spec:
        n = len(recs or [])
        add("max_records", n <= spec["max_records"], f"{n} <= {spec['max_records']}")
    for key, want in (("path_exists", True), ("path_absent", False)):
        if key in spec:
            paths = resolve_paths(spec[key], inputs)
            missing = [p for p in paths if os.path.lexists(p) != want]
            add(key, not missing, f"violations: {missing}" if missing else f"{len(paths)} path(s)")
    if spec.get("no_failed") and isinstance(data, dict):
        add("no_failed", not data.get("failed"), f"failed: {data.get('failed')}" if data.get("failed") else None)
    if "stdout_regex" in spec:
        add("stdout_regex", re.search(spec["stdout_regex"], stdout or "", re.M) is not None)
    if "field_equals" in spec and isinstance(data, dict):
        for k, v in spec["field_equals"].items():
            add(f"field_equals:{k}", data.get(k) == v, f"{data.get(k)!r} == {v!r}")
    if "record_count_equals" in spec:
        n = len(recs or [])
        add("record_count_equals", n == spec["record_count_equals"], f"{n} == {spec['record_count_equals']}")
    passed = all(c["passed"] for c in out)
    evidence = any(c["passed"] for c in out if c["check"] != "exit_in")
    return {"passed": passed, "evidence": evidence, "checks": out}


def resolve_paths(template, inputs):
    if isinstance(template, str) and template.startswith("${") and template.endswith("}"):
        v = inputs.get(template[2:-1])
    else:
        v = template
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)
