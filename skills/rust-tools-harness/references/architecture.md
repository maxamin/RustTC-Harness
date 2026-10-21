# Architecture

It's local files and one Python CLI: no daemon, database or service, and no LLM or MCP dependency. The MCP server
(`rth mcp`) and the standalone agent (`rth agent`, optional local model via `--llm`) are front ends that start only when invoked. Every component is a module in
`lib/rth/` and they exchange plain JSON.

```
README.md ──parse──▶ registry/catalogue.json ──┐
upstream (GitHub, Cargo.toml, crates.io) ─fetch─▶ registry/upstream.json ──┼─build─▶ registry/tools.json ─docs─▶ references/catalogue.md
registry/curated.json (hand-written claims) ────┘
registry/capabilities.json (taxonomy: keywords, native fallbacks, semantic gaps)

intent ─▶ matcher ─▶ planner ─▶ plan.json ─▶ executor ─▶ result.json + audit.jsonl
             ▲           ▲                      │  ├─ env (which/identity/--help cache)
             │      workflows/*.json            │  ├─ ops (adapters: build argv, parse output)
         registry                               │  ├─ validate (evidence checks)
                                                │  └─ recover (classify, retry/fallback/stop)
```

| component | module | role |
|---|---|---|
| Registry | `registry.py`, `registry/*.json` | searchable catalogue + capability taxonomy |
| Capability matcher | `matcher.py` | intent → capabilities (keywords) → ranked candidates |
| Environment detector | `env.py` | OS, distro, arch, shell, TTY, CI, privileges (root, CapEff, perf_event_paranoid, ptrace_scope), runtimes, package managers, binaries; lazy `--version`/`--help` probes; identity checks |
| Planner | `planner.py`, `workflows/` | objective → workflow instance or single-op plan; parameter defaults/guessing; `needs-input` |
| Dispatcher | `executor.run_step` | risk gate → first usable impl → run → parse → verify → recover |
| Workflow engine | `executor.execute` | dependency waves, `${params.*}` / `${steps.<id>.data.*}` references, `when` conditions, `verified_by` |
| Result normalizer | `ops.py` parsers | tool output → records (`matches`, `results`, `files`…); output over 50 records is truncated inline, full output kept in the run dir |
| Validator | `validate.py` | `exit_in`, `output_parsed`, `not_empty`, `min_records`, `max_records`, `path_exists`, `path_absent`, `no_failed`, `stdout_regex`, `field_equals`, `record_count_equals` |
| Recovery engine | `recover.py` | missing-binary, identity-conflict, unsupported-flag, cannot-express, timeout, malformed-output, permission-denied, network-error, not-found |
| Undo | `undo.py` | snapshot before write steps, before/after hashes after them, all-or-nothing rollback that moves instead of deleting |
| API / MCP | `api.py`, `mcp.py` | functions shared by the CLI and the stdio MCP server (`bin/rth-mcp`) |
| Standalone agent (opt-in) | `agent.py`, `rth agent`, `bin/rth-agent` | default: deterministic planner + in-process tools (no LLM, no MCP). `--llm ollama/openai` adds a local model (default Qwen Coder); `--mcp` routes tool calls through a stdio MCP server. Local-endpoint guard, approval policy, harness-checked verification, JSONL transcript |
| Portable fallbacks | `builtins.py` | pure-Python impls for core ops (walk/search/tree/du/archives/diff/copy, jq subset, graveyard delete) |
| Extra ops | `ops_extra.py` | csv-query (qsv sqlp / sqlite3), fs-query, http-test, cpu-profile, python-profile, syscall-trace, net-bandwidth, resource-history |
| Learning | `history.py` | per (op, tool) outcomes in `history.jsonl`; persistent failures → `learned-skip` |
| Sandbox | `sandbox.py` | prlimit resource limits (default for user code) + optional bubblewrap isolation |
| Locks | `locks.py` | flock per path (exclusive) + ancestors (shared) for write steps |
| Audit log | `audit.py` | `runs/<id>/audit.jsonl` (run_start, step_start, attempt with argv/exit/seconds/stderr tail, gate, step_end, run_end) + stdout/stderr artifacts + `result.json` |

## Registry provenance

`tools.json` merges three sources and records where each claim came from:

- `verification.repo_reachable`, `redirected_to`, `archived_on`, `last_commit`, `latest_release` come from GitHub HTML, the commits Atom feed and the releases redirect (no token needed).
- `verification.crate`, `crate_version`, `crate_bins` come from crates.io. A crate is accepted **only** if its `repository`/`homepage` points at the same GitHub repo.
- `verification.cargo_toml_bins` come from the root `Cargo.toml` and explicitly listed workspace members.
- `binary_source`: `crates.io` / `Cargo.toml` (upstream-verified), `curated` (hand-written claim), or `unknown` (in which case `binary` is null).
- `status`: `archived` (GitHub banner), `inactive` (no commit for >2 years), `experimental` (curated: alpha or very new), `unverified` (repo unreachable), otherwise `active`.

Everything else (capabilities, flags, interactivity, automation, risk, limitations, alternatives) is
curated in `curated.json`; `unknown` means not established. Flags marked `(vd)` are version-dependent, and
adapters check `--help` before using them (e.g. `dust --output-json`).

Refresh: `scripts/build_registry.py parse && scripts/build_registry.py fetch --refresh && scripts/build_registry.py build` (build also regenerates `references/catalogue.md`).
`fetch --crates-only` redoes only the crates.io matching.

## Schemas

**Plan** (`rust-tools-harness/plan@1`):
```json
{"objective": "...", "workflow": "search-replace", "params": {...}, "status": "ready|needs-input",
 "steps": [{"id": "apply", "purpose": "...", "op": "replace-apply", "tool": null,
            "inputs": {"files": "${steps.preview.data.files}"}, "depends_on": ["preview"],
            "preconditions": {"any_of_tools": ["sd", "perl"], "risk": "writes-files", "approval": "..."},
            "expected_output": "...", "timeout": 120, "on_failure": "fallback|retry|stop|continue",
            "verification": {"exit_in": [0]}, "verified_by": ["after"], "critical": true, "when": null}]}
```
`tool` pins a preferred implementation; fallbacks still apply unless `on_failure` is `stop`.
`critical: false` steps don't affect the overall status. `when` skips a step as `not-applicable` when it resolves falsy.

**Result** (`rust-tools-harness/result@1`): `run`, `status` (`verified`, `completed-unverified`,
`partial`, `failed`, `needs-approval`, `needs-input`, `planned`), `steps[]` (`status`, `tool`, `exit`, `data`,
`truncated`, `semantics`, `verification.checks[]`, `attempts[]`, `recovery`), `remaining[]`, `run_dir`.

## Execution rules

- Read-only steps in the same dependency wave run concurrently (4 workers). Everything else runs sequentially.
- Subprocesses get their own session; on timeout the whole process group gets SIGTERM, then SIGKILL.
  `NO_COLOR=1`, `PAGER=cat` and `GIT_PAGER=cat` stop pagers from hanging.
- Retry: only timeouts on read-only steps, once, with double the timeout. Other failures fall back to the next
  implementation with the same capability. `not-found` and `network-error` stop, because changing tools won't fix the input.
- Permission failures fall back only to other unprivileged implementations. The harness never runs `sudo`.
- Status `verified` requires at least one passing check beyond the exit code.
- Long steps stream output: heartbeats go to the audit log (and stderr with `--progress`).
- `--resume RUN` reuses steps whose definition hash matches and whose status was `verified`/`not-applicable`, only
  if that run finished less than an hour ago.

## Extending

Add an op in `ops.py`:
```python
op(Op("my-op", "<capability>", "summary", {"path": ".", "x": "REQUIRED ..."},
      [Impl("rusttool", build=lambda p, env: [...], parse=parse_fn, registry_id="<tool id>", ok_exit=(0, 1),
            when=lambda p, env: None or "reason", semantics="how it differs"),
       Impl("native", build=..., parse=...)],
      risk=READ_ONLY, verify={"min_records": 1}))
```
`build` returns `None` when the implementation cannot express the parameters faithfully, which is better than an approximate substitute.
`parse` should raise `ValueError` on unexpected output, so the executor falls back instead of returning garbage.
Write ops should also pass `undo=lambda p: {"modify": [...], "create": [...]}` so the step gets a snapshot, rollback and
before/after evidence. Add a fixture under `tests/fixtures/` and a case in `tests/test_parsers.py` for each new parser, and a
case in `tests/test_live.py`'s `cases()` for each new op.
Add a workflow by dropping a JSON file in `workflows/` (see existing ones). `tests/test_harness.py` checks that ops,
capabilities, workflows and the registry stay consistent.
