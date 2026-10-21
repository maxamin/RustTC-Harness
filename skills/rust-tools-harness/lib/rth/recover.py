"""Recovery engine: classify failures and decide retry / fallback / stop."""
import re

PATTERNS = [
    ("unsupported-flag", r"unexpected argument|unrecognized (option|argument)|unknown (option|flag|argument)|invalid option|"
                         r"illegal option|wasn't expected|no such option|unknown command|unrecognized subcommand"),
    ("permission-denied", r"permission denied|operation not permitted|EACCES|EPERM|must be (run as )?root|requires root|"
                          r"CAP_NET_RAW|CAP_NET_ADMIN|insufficient privilege|are you root|perf_event_paranoid|ptrace"),
    ("network-error", r"could not resolve|name or service not known|temporary failure in name resolution|"
                      r"network is unreachable|no route to host|connection refused"),
    ("not-found", r"no such file or directory|not a git repository|does not exist|cannot access"),
]

# what to do for each class: (retry_same, try_fallback, stop)
POLICY = {
    "missing-binary": (False, True, False),
    "identity-conflict": (False, True, False),
    "unsupported-flag": (False, True, False),
    "cannot-express": (False, True, False),
    "timeout": (True, True, False),
    "malformed-output": (False, True, False),
    "permission-denied": (False, True, True),
    "network-error": (True, False, True),
    "not-found": (False, False, True),
    "failed": (False, True, False),
    "needs-approval": (False, False, True),
    "learned-skip": (False, True, False),
    "sandbox-unavailable": (False, False, True),
}

ADVICE = {
    "missing-binary": "Tool not installed; using the next implementation with the same capability. Install only if the user wants it.",
    "identity-conflict": "Binary name on PATH belongs to a different tool; skipped.",
    "unsupported-flag": "This installed version rejects a flag; check `rth help <tool>` and fall back.",
    "cannot-express": "This implementation cannot express the requested parameters without changing semantics.",
    "timeout": "Timed out; retried once with a longer limit if the step is read-only, then fell back.",
    "malformed-output": "Output did not match the expected format (version drift?); fell back.",
    "permission-denied": "Needs privileges. Do not escalate automatically: ask the user, or use an unprivileged alternative.",
    "network-error": "Network/DNS failure; check the target and connectivity before retrying.",
    "not-found": "Input path/repo does not exist; fix inputs rather than switching tools.",
    "failed": "Command failed; inspect stderr before deciding.",
    "needs-approval": "Step changes state; rerun with --allow/--approve once the user authorizes it.",
    "learned-skip": "Skipped: this tool failed persistently here before (see `rth history`); RTH_NO_LEARN=1 or --no-learn retries it.",
    "sandbox-unavailable": "--sandbox requested but bubblewrap is unavailable; install bwrap or rerun without --sandbox (resource limits still apply).",
}


def classify(*, exit_code=None, stderr="", timed_out=False, parse_error=None, ok_exit=(0,), missing=False):
    if missing:
        return "missing-binary"
    if timed_out:
        return "timeout"
    if parse_error is not None and exit_code in ok_exit:
        return "malformed-output"
    if exit_code in ok_exit:
        return None
    if exit_code in (126, 127) and not stderr:
        return "missing-binary"
    for name, pat in PATTERNS:
        if re.search(pat, stderr or "", re.I):
            return name
    return "failed"


def decision(kind):
    retry, fallback, stop = POLICY.get(kind, (False, True, False))
    return {"class": kind, "retry": retry, "fallback": fallback, "stop": stop, "advice": ADVICE.get(kind, "")}
