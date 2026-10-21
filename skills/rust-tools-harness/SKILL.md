---
name: rust-tools-harness
description: Capability-based harness over the Awesome Rust Tools catalogue (174 tools, verified against upstream) plus native Unix fallbacks. Use for repository analysis, code search, file operations, find/replace, disk usage, data (CSV/JSON) processing, system and network diagnostics, benchmarking, git review and automation. It discovers what is installed, selects tools by capability, runs them with timeouts and fallbacks, normalizes output to JSON, verifies results with evidence, and writes an audit log. Use it when a task maps to these capabilities, or when the user asks which Rust CLI tool fits a job.
---

# Rust Tools Harness

Request **capabilities**, not commands. The harness works out which tool can deliver
a capability on this machine, runs it safely and returns evidence.

`$H` below means `python3 <dir containing this SKILL.md>/bin/rth`. It uses only the Python 3.9+
standard library and needs no install. State (env cache, run logs) goes in
`$RTH_STATE`, default `~/.cache/rust-tools-harness`.

**The harness needs no LLM and no MCP.** You (the agent reading this) are the intelligence, and you
drive `$H` from the shell, which is the normal way to use this skill. Two optional front ends exist and start
only when explicitly invoked: `$H mcp` (stdio MCP server) and `$H agent <goal> [--llm ...]` (a standalone
agent for running a goal without you). Don't start either unless the user asks for it.

## Loop: OBSERVE → PLAN → SELECT → EXECUTE → INSPECT → VERIFY → REPLAN

1. **Observe**: `$H env` gives OS, shell, privileges, runtimes, and which catalogue and native
   binaries exist. It is cached for 1 hour and invalidated when PATH changes; use `--refresh-env` after installing something.
2. **Plan**: `$H plan "<objective>" [-p key=value ...] -o "$RTH_STATE/plan.json"`. The planner matches a
   workflow template or builds a single-op plan. Check `status` (`ready` / `needs-input`) and
   `missing_params`. Pass parameters explicitly: guessing from the objective text is only a convenience.
   Write plan files **outside** the tree you are about to modify.
3. **Select** (when no plan fits): `$H select "<intent>"` gives the best executable op and the tool it
   will use here. `$H discover "<intent>"` gives capabilities, ranked catalogue and native candidates,
   and semantic gaps. Use discover for tools the harness has no adapter for, then run them yourself.
4. **Execute**: `$H execute plan.json --brief`, or a single op: `$H run <op> -p k=v [--brief]`.
   Independent read-only steps run concurrently. Every attempt has a timeout.
5. **Inspect**: drop `--brief` to see the normalized `data`. Each step reports `tool`, `semantics`
   (how this tool's behaviour differs from the reference) and every attempt, including skipped and failed ones.
6. **Verify**: `$H verify <run-id|result.json>` lists the evidence for each step. Statuses:
   `verified` (passed checks beyond the exit code), `completed-unverified` (ran, no evidence),
   `verification-failed`, `failed`, `needs-approval`, `needs-input`, `blocked`, `skipped`, `not-applicable`.
   **Never report success from an exit code alone**, and confirm the evidence answers the user's question.
7. **Replan**: on `failed`, read `recovery.advice`. `$H recover --tool X --stderr "..." --exit N`
   lists installed alternatives with the same capability. Stop when the objective is met, progress
   is impossible, or the next step needs approval.

Exit codes of `run`/`execute`: 0 = verified or planned, 3 = needs approval or input, 1 = anything else.

## Commands

| need | command |
|---|---|
| list capabilities (with ops, installed tools, native fallbacks) | `$H capabilities [--executable] [--fast]` |
| search the catalogue | `$H search <words> [--installed] [--category Search] [--kind cli]` |
| one tool's full record + local identity | `$H info <tool>` |
| executable ops / one op's params | `$H ops [<op>]` |
| workflow templates | `$H workflows [<name>]` |
| tool usage when syntax is uncertain | `$H run help -p tool=<binary>` |
| preview without running | add `--dry-run` to `run`/`execute` |
| past runs / audit trail | `$H audit [<run-id>]` |
| undo a run's write steps | `$H rollback <run-id>` (preview), then `--apply` |
| registry and environment sanity (incl. freshness, README sync) | `$H doctor` |
| what the harness learned on this machine | `$H history [<op>]` |
| rerun a plan, reusing verified steps | `$H execute plan.json --resume <run-id>` (runs < 1 h old) |
| long steps: show heartbeats | add `--progress` |

Ops (40): find-files, search-content, view-file, hex-view, list-dir, tree, disk-usage, dir-size, disk-free,
process-list, system-info, json-query, csv-profile, csv-query (SQL over CSV), fs-query, code-stats, repo-summary,
git-status, git-log, git-diff-stat, diff, benchmark, cpu-profile, python-profile, syscall-trace, replace-preview,
replace-apply, archive-list, archive-extract, copy, safe-delete, net-connections, net-bandwidth, trace-route,
port-scan, http-test, resource-history, task-list, parallel, help.

Every core op ends with a pure-Python fallback, so it still works with no external tools (minimal containers,
Windows). The fallback states its differences in `semantics`, e.g. it doesn't honor `.gitignore`, and the JSON
fallback supports only a jq subset.

Workflows: repo-intel, disk-usage-report, search-replace, data-profile, system-diagnostics,
network-path, benchmark-compare, git-review, archive-inspect. See `references/workflows.md`.

## Approval gates

Steps that change state stop with `needs-approval` until the user authorizes them:

| risk | flag | examples |
|---|---|---|
| writes-files | `--allow writes` | replace-apply, copy, archive-extract, safe-delete |
| network-active | `--allow network` | trace-route, port-scan |
| runs-user-code | `--allow exec` | benchmark, parallel |
| needs-privilege | `--allow privileged` | (never auto-sudo; the harness does not escalate) |

`--approve <step-id>` authorizes a single step. Write steps (replace-apply, copy, archive-extract) snapshot
what they touch first. The step result carries `undo.rollback`, and `effect` (before/after hashes) is the
evidence that the write happened. `rth rollback <run> --apply` restores modified files and moves created
paths into the run directory (never deletes). It is all-or-nothing: if a file changed after the run it
reports a conflict and does nothing unless `--force`. safe-delete is undone with the trash tool itself
(`trash restore`, `rip -u`). Ask the user before passing any of these unless they
already asked for that action. Never pass `--allow` just to get past a gate.

User-code steps (benchmark, parallel, profilers, syscall-trace) run under resource limits by default (CPU 900 s,
memory 4 GB, file size 2 GB, via `prlimit`). Add `--sandbox` to isolate them with bubblewrap: no network,
read-only filesystem except the working directory and run directory, private TMPDIR. The step's `sandbox` field
shows what applied. Write steps lock the paths they touch, so concurrent runs can't modify the same files. A
conflicting step comes back `blocked` with the holder's run id.

Hard rules:
- `safe-delete` only moves files somewhere recoverable: trashy, rip, gio, trash-put, or the harness graveyard in
  the run directory (restore with `rth rollback <run> --apply`). **Do not substitute `rm`.**
- `port-scan` is blocked unless `authorized_scope` (host/CIDR the user authorized) contains the target.
  An open-port list is not a security assessment.
- `archive-extract` refuses non-empty destinations. `copy` never overwrites.
- Never discard user changes or rewrite git history (`git absorb --and-rebase`, reset, rebase) without
  explicit authorization. Check `git-status` before any write workflow.

## Optional front ends (opt-in only)

| front end | starts with | what it is |
|---|---|---|
| MCP server | `$H mcp` or `bin/rth-mcp` | exposes the harness as 16 MCP tools over stdio. The plain tools are read-only; `rth_run_approved`, `rth_execute_approved` and `rth_rollback_apply` take an `allow` list and are separate so client permission prompts can tell them apart |
| standalone agent | `$H agent "<goal>"` | deterministic planner, no model: plan → execute → approval → verify → report |
| ...with a local LLM | `$H agent "<goal>" --llm ollama` (or `--llm openai --url http://127.0.0.1:8080`) | a local model chooses the tools (default Qwen Coder, `qwen2.5-coder:7b`, or any installed `qwen*coder*`). Non-local endpoints are refused |
| ...through MCP | add `--mcp` (bundled server) or `--mcp "<cmd>"` | the agent's tool calls go over a real MCP connection instead of in-process calls |

If the rth MCP server is already connected in your client, you may call its tools instead of the shell, with the
same rules. With `agent`, pass `--allow` only for risk classes the user authorized, and report the verification block
it prints (checked by the harness), not just the answer.

## Selection principles

- Match by capability. The registry ranks tools on installed + identity-confirmed, automation
  suitability, structured output, harness adapter, maintenance status, and risk. Popularity is not a factor.
- When the preferred tool is missing, `select` adds an `upgrade` hint and `info` an `install_here` list, using
  only package managers present on this machine. Installing is the user's call: suggest it, don't do it.
- The harness learns: a tool that failed 3 times in a row here for a persistent reason (permissions, unsupported
  flag, wrong binary, unparseable output) is skipped next time (`learned-skip`). See `$H history`; `--no-learn` retries.
- Never assume a registry tool is installed. Binary names collide (`hx` = helix or hex; `tldr` =
  tealdeer or tlrc; Debian ships `fd` as `fdfind` and `bat` as `batcat`). The harness probes
  `--version` and reports `confirmed` / `unconfirmed` / `conflict`.
- Use Rust tools where they bring a real advantage: speed on big trees, `.gitignore` awareness, JSON
  output, structural diffs, statistically sound benchmarks. Plain numbered text from `awk` beats `bat` for an
  agent reading a file, and the adapters already make that choice.
- Fallbacks keep the meaning of the operation. When a fallback differs (e.g. `find` ignores `.gitignore`,
  `grep -E` is POSIX ERE, `du` vs `dust` precision), the step's `semantics` says so: **pass that on** when it
  affects the answer.
- Interactive TUIs (bottom, gitui, yazi, television, csvlens, jless) need a human at a TTY. Use their
  non-interactive equivalents in automation; `discover` marks them `interactive-only, no TTY`.
- For tools without an adapter (e.g. `qsv sqlp`, `cargo flamegraph`, `py-spy record`, `hurl --test`), read
  `$H info <tool>` (flags, exit behaviour, limitations), check `--help`, run the smallest useful command
  yourself, and verify the artifact it produces.

## Reporting

Report: what was answered, the evidence (counts, paths, measurements and the tool that produced them),
semantic caveats from `semantics`, anything `remaining` (unverified, blocked or needs-approval), and the run id
for the audit trail. For benchmarks, claim a difference only if it exceeds the combined stddev under
identical settings.

## References

- `references/architecture.md`: components, data flow, plan/step/result schemas, extending ops.
- `references/workflows.md`: each workflow's steps and how to compose manual pipelines (data,
  performance, diagnostics, network, git, automation).
- `references/catalogue.md`: all 174 tools with binary, kind, status, automation and capabilities (generated).
- `registry/tools.json`: the machine-readable registry, with per-tool `verification` provenance.
