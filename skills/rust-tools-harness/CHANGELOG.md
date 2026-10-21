# Changelog

## 1.3.0
- Composition: compound objectives → chained plans with output→input wiring and `foreach` (max 50 items).
- Remote targets (`--target ssh:HOST|docker:NAME|podman:NAME`) for read-only ops.
- Git writes: git-commit, git-branch (writes), git-absorb (new `history` gate); non-destructive undo.
- 15 more ops (grex, fend/eva, lemmeknow, pastel, task-run, qsv/xsv frequency+select, zola, sccache, jj, himalaya, absorb preview).
- Request understanding: stemming, synonyms, op-level keywords, file-extension hints, arithmetic words; 70-case routing set
  (100%), unseen sets measured at 80% and 73% before their fixes; CI fails below 95%.
- jq subset ~3x larger (sort_by, group_by, unique, add, min/max, to_entries, join, select and/or/test/has, objects) — 19/19 match jq.
- `rth gc` (auto daily, keeps graveyards) and `rth stats`.
- Sandbox: `--sandbox-readonly`, ulimit fallback (macOS/BSD), macOS sandbox-exec (untested); Windows msvcrt locks (untested).
- Rollback restores gio-trashed files automatically.
- CI: Windows job for portable tests.

## 1.2.0
- 40 ops (new: csv-query, fs-query, http-test, cpu-profile, python-profile, syscall-trace, net-bandwidth, resource-history).
- Pure-Python fallbacks for every core op (works without external tools; Windows tasklist for processes; builtin jq subset).
- Sandbox: prlimit resource limits by default for user code; `--sandbox` bubblewrap isolation.
- Path locks for write steps; learned tool skipping (`rth history`); `--resume`; `--progress` heartbeats.
- Install hints filtered to this machine's package managers (`select` upgrade, `info` install_here).
- Plain-language constraint extraction (sizes, time windows, file types, limits, ports, durations, paths).
- Rollback: safe-delete via graveyard, directory change detection, all-or-nothing.
- Registry: 161/174 binaries confirmed upstream (crates.io or Cargo.toml build targets), 0 curated-only;
  `verify-bins`, freshness + README sync in `doctor`; weekly refresh workflow.
- Opt-in front ends: `rth mcp`, `rth agent [--llm ollama|openai] [--mcp]` (no LLM/MCP by default).
- Small-model affordances: param aliases, instructive errors, `next` suggestions, repeat-call nudges.
- Installer: `--copy`, `--codex-dir/--claude-dir`, `--check`, `--uninstall`; Ollama user-service script.

## 1.1.0
- Undo/rollback for write steps, MCP server, real-binary live tests, parser fixtures, CI workflow.

## 1.0.0
- Registry of the 174-tool catalogue, capability matcher, environment detector, planner, executor with
  fallbacks/verification/recovery, audit log, 9 workflows.
