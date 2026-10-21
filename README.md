# RustTC-Harness

[![rust-tools-harness](https://github.com/maxamin/RustTC-Harness/actions/workflows/rust-tools-harness.yml/badge.svg)](https://github.com/maxamin/RustTC-Harness/actions/workflows/rust-tools-harness.yml)

**A capability-based tool harness for AI coding agents.** It turns the
[Awesome Rust Tools](CATALOGUE.md) catalogue (174 tools) and the usual Unix utilities into one interface
that an agent such as Claude Code or Codex can use to **discover, select, plan, execute, verify and recover**.

Agents ask for capabilities, not commands: *"where is `parse_config` used"*, *"what is eating my disk"*,
*"find rust files then show each of them"*. The harness detects what's installed, picks the best tool for the job
(`rg` → `git grep` → `grep` → pure Python), runs it with timeouts and fallbacks, normalizes the output to JSON,
checks the result for evidence, and logs every action.

```
"what is using disk space in ./target"
        │
        ▼
  matcher ─▶ planner ─▶ plan.json ─▶ executor ─▶ result.json + audit log
  (intent)   (workflow /   (steps,      ├─ env: what's installed, which binary is really which
              composed)     gates)      ├─ ops: dust → du → Python, each with stated semantics
                                        ├─ validate: evidence beyond exit code 0
                                        └─ recover: retry, fall back or stop
```

## Highlights

- **Verified registry**: all 174 catalogue entries are checked against upstream (GitHub status, latest commit,
  releases, crates.io, Cargo.toml build targets). 161 binary names are confirmed upstream. The rest stay
  `unknown`, never guessed. A weekly workflow re-checks everything.
- **55 executable ops, 9 workflows**: search, files, disk, processes, network, CSV/JSON/SQL, profiling,
  benchmarking, archives, git review and git writes, task runners. Every core op has a **pure-Python fallback**,
  so it also works on minimal systems.
- **Composition**: *"list logs then move them to the trash"* becomes a chained plan. Paths are wired between steps,
  and `foreach` runs a step once per result.
- **Safety by design**:
  - approval gates for writes, network, running code and history rewrites;
  - undo/rollback that never deletes (no `rm` fallback);
  - resource limits and optional bubblewrap isolation for user code;
  - path locks between concurrent runs;
  - an authorization scope for port scans;
  - no privilege escalation.
- **Adaptive**:
  - learns which tools fail on this machine;
  - resumes plans from verified steps;
  - extracts constraints from plain language ("python files bigger than 1MB changed in the last 2 days");
  - suggests installs using only the package managers you have.
- **No dependencies**: Python 3.9+ standard library only. No daemon, database or LLM needed.
- **Optional front ends**: a stdio MCP server, and a standalone offline agent that can use a local model
  (Qwen Coder via Ollama or any OpenAI-compatible local server). Both start only when you ask for them.

## Quick start

```sh
git clone git@github.com:maxamin/RustTC-Harness.git && cd RustTC-Harness

# install the skill (symlink; --copy for a standalone copy)
skills/rust-tools-harness/scripts/install.sh claude        # ~/.claude/skills
skills/rust-tools-harness/scripts/install.sh codex         # ~/.codex/skills (or --codex-dir DIR)
```

Claude Code and Codex pick it up from `SKILL.md` in the next session. You can also use the CLI directly:

```sh
H="python3 skills/rust-tools-harness/bin/rth"
$H env                                              # what this machine has
$H select "where is parse_config used"              # best executable op + tool here
$H run search-content -p pattern=parse_config       # run one op → JSON with evidence
$H plan "find rust files then show each of them" -o /tmp/plan.json
$H execute /tmp/plan.json --brief                   # gated steps stop at needs-approval
$H verify <run-id>                                  # evidence per step
$H rollback <run-id> --apply                        # undo a run's writes
```

## Safety model

| risk | how to allow it | examples |
|---|---|---|
| writes files | `--allow writes` / `--approve STEP` | replace, copy, extract, safe-delete, git commit |
| network | `--allow network` | traceroute, port scan (scope-checked), HTTP tests |
| runs user code | `--allow exec` (resource-limited; `--sandbox` to isolate) | benchmark, profilers, task-run |
| rewrites history | `--allow history` | git absorb |
| privileged | never escalated automatically | |

Write steps snapshot what they touch first. Rollback is all-or-nothing and refuses to clobber later edits.

## Repository layout

```
skills/rust-tools-harness/   the skill: SKILL.md, bin/rth, lib/rth/, registry/, workflows/, references/, tests/
CATALOGUE.md                 the Awesome Rust Tools list: the registry's source dataset
.github/workflows/           CI (unit, Windows, live adapters on Linux+macOS) and weekly registry refresh
```

Start with [`skills/rust-tools-harness/README.md`](skills/rust-tools-harness/README.md) for usage, and
[`references/architecture.md`](skills/rust-tools-harness/references/architecture.md) for internals and extending
ops. The full tool table is in [`references/catalogue.md`](skills/rust-tools-harness/references/catalogue.md).

## Status

- 136 tests pass locally (Linux): registry consistency, parser snapshots of real tool output, live runs of every
  installed adapter, empty-PATH fallbacks, rollback, locks, sandbox, composition, remote and git writes, and a
  70-case intent-routing set.
- **Not yet verified:** macOS, Windows, and the adapters for dust, diskus, procs, hexyl, ouch, difftastic and xcp.
  CI covers them; the badge above shows their status.
- The local-LLM agent mode is experimental. A 7B model on CPU did not complete a task end to end in testing. The
  harness itself doesn't depend on it.

## Credits & license

The tool catalogue comes from [unpluggedcoder/awesome-rust-tools](https://github.com/unpluggedcoder/awesome-rust-tools)
and its contributors. MIT licensed: see [LICENSE](LICENSE).
