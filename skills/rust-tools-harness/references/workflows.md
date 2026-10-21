# Workflows

The templates in `workflows/*.json` are starting patterns. Instantiate one with
`rth plan --workflow <name> -p k=v -o plan.json`, edit the plan if the task needs it (add, remove or pin steps),
then `rth execute plan.json`. Where no template fits, compose ops by hand, or run catalogue tools
directly using `rth info <tool>`.

## repo-intel (read-only)
`repo-summary` → `code-stats` (tokei → builtin) → `tree` (eza → tree → find) → `task-list` → `git-status`
→ `search-content` (rg → git grep → grep, only if `pattern` given).
Manual extensions: `rg --json` on symbol definitions; `bat -r A:B` for human display; `difft` for change review;
`television`/`skim`/`fzf` only when a human is choosing interactively (`sk --filter` works without a TTY).

## disk-usage-report (read-only)
`disk-free` (is a filesystem actually full?) ∥ `dir-size` (diskus → du) ∥ `disk-usage` (dust JSON → du -k) ∥
`find-files type=f min_size=100M no_ignore hidden`.
Explain with evidence: mount use%, biggest directories in bytes, large files. Deleting is a separate step: `safe-delete`,
which needs approval. Interactive alternatives for a human: `dua i`, `diskonaut`, `broot --sizes`.
Units: fd `-S` uses SI units (M = 10^6) but find `-size` uses MiB, and the step's `semantics` says which applied.

## search-replace (write, gated)
`search-content` baseline → `replace-preview` (`rg -r`, writes nothing) → **`replace-apply`** to exactly the previewed files
(sd → perl; perl refuses `$name` captures it can't map) → `search-content` after (expects 0 matches; this step is the
`verified_by` evidence for apply) → `git-diff-stat`.
Before applying: check `git-status` so the change can be reviewed or reverted, and put the plan file outside the target tree.
If the replacement contains the pattern, the `after` check will fail by design, so edit it to `min_records`.
Interactive alternatives: `repgrep` (rgr), `ruplacer` (dry-run by default, `--go` to write; archived upstream since Feb 2026).

## data-profile (read-only)
`view-file` (first lines) → `csv-profile` (qsv stats → xsv stats → builtin csv) or `json-query` (jaq → jq → builtin identity).
Manual extensions: `qsv frequency -s COL`, `qsv sqlp FILE 'SELECT …'` (feature-dependent: check `qsv --list`),
`qsv validate schema.json`, `fselect "select path, size from . where name like '%.csv' into json"`,
`jaq -c '.items[] | {id, status}'`. Prefer structured output over text parsing. `jless` and `csvlens` are for humans.

## system-diagnostics (read-only)
`system-info` ∥ `process-list sort=cpu` ∥ `process-list sort=mem` ∥ `disk-free` ∥ `net-connections listening`.
Method: form a hypothesis from the snapshot, gather targeted evidence (`process-list filter=NAME`,
`disk-usage path=/var`, `below dump system -O json` for history if `below record` was running), and confirm before
concluding. On Linux, ps %CPU is a lifetime average, so for "right now" sample twice or read `top -b -n1`.
Network usage per process (`bandwhich --raw`, `rustnet`, `nethogs`) needs privileges; ask first.

## network-path (network, gated)
`net-connections` (passive) → `trace-route` (trippy `-m json` → mtr `--json` → traceroute → tracepath).
Keep the kinds of action separate: passive observation (net-connections, dnspeep, sniffnet), connectivity checks
(trace-route), service discovery and active scanning (`port-scan`: rustscan → nmap, which requires `authorized_scope`;
netscanner is interactive). Scan only systems the user is authorized to test. A scan result is never proof of security.

## benchmark-compare (exec, gated)
`benchmark commands=[baseline, candidate]`: hyperfine with the same `--runs`/`--warmup`/`--prepare` for both, exported to
JSON. The builtin fallback (Python loop) includes shell overhead and has no outlier detection, so treat it as indicative only.
Performance loop: baseline → profile (`cargo flamegraph -o fg.svg -- <args>` or `perf record` + `inferno-collapse-perf | inferno-flamegraph`,
`py-spy record -o out.svg --pid N` or `-- python app.py`) → change → measure again with identical settings.
Claim an improvement only if `|Δmean| > stddev_a + stddev_b`. Profilers may need `perf_event_paranoid ≤ 1` or ptrace
permission, which `rth env` reports.

## git-review (read-only)
`git-status` → `git-diff-stat` → `diff` (difftastic via `git -c diff.external=difft diff --ext-diff` → `git diff`) →
`git-log` → `task-list` (which checks to run). Then run the project's tests and lints (just, make, cargo, npm), which runs code and needs approval.
`delta` and `riff` are presentation-only pagers for humans. `git-absorb` creates fixup commits, and with
`--and-rebase` rewrites history: only on explicit request. Use `jj` only in repos the user already manages with jj.
`gitoxide` (`gix`) covers part of git's plumbing, so prefer `git` when correctness matters.

## archive-inspect (write only for extract)
`archive-list` (ouch → tar → unzip) → `archive-extract` into a new directory (ouch `--no` → tar `-k` → unzip `-n`).

## Ops added for these workflows
- `csv-query`: `SELECT ... FROM data` over a CSV (qsv sqlp, else sqlite3 in memory; read-only authorizer).
- `cpu-profile`: flamegraph SVG (cargo-flamegraph, else perf + inferno). `python-profile`: py-spy speedscope, else cProfile top functions.
- `syscall-trace`: lurk, else `strace -f -c` parsed into per-syscall counts, time and errors.
- `net-bandwidth`: bandwhich per process (privileged, so skipped unless `--allow privileged`), else `/proc/net/dev` per interface.
- `http-test`: `hurl --test --json`, else a single urllib request with status/body assertions (`--allow network`).
- `resource-history`: `below dump` (JSON), else `sar`. `fs-query`: fselect `into json`.

## Automation patterns
- Discover: `rth run task-list` finds just, mask, make, npm, cargo and mise tasks. `just --dump --dump-format json` is machine-readable.
- Fan out independent commands: `rth run parallel -p 'commands=["cmd1","cmd2"]' --allow exec`. Each command keeps its own
  exit code and output. `rust-parallel` (`--tool rust-parallel`) is faster for thousands of commands but only reports an aggregate exit code.
- `mprocs` (now `dekit` upstream) and `zellij` are interactive: use them for humans running long-lived services, not for agent pipelines.
- `navi --print` / `--best-match` and `tldr` give command examples. Always check `--help` on the installed version.
- Make repeatable steps idempotent: copy without clobbering, extract only into empty directories, preview before
  replacing, check `git-status` before and after.
