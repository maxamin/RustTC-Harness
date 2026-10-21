# rust-tools-harness

An agent skill for Claude Code and OpenAI Codex. It turns the
[Awesome Rust Tools](https://github.com/unpluggedcoder/awesome-rust-tools) catalogue into a capability-based
tool harness: the agent asks for *search code by regex* or *what is using disk space*, and the harness
discovers what is installed, picks a suitable tool, runs it with timeouts and fallbacks, normalizes the output to
JSON, verifies the result with evidence, and logs every action.

- **Registry**: all 174 catalogue entries, checked against upstream (GitHub status, latest commit, release, crates.io
  crate matched by repository URL, Cargo.toml build targets). 161 binary names are confirmed upstream and none rest on
  hand-written claims. The other 13 are GUIs, libraries or ambiguous, and stay `unknown`. A weekly workflow
  (`registry-refresh.yml`) re-checks everything and opens a PR.
- **55 executable ops** with ranked Rust and native implementations, plus pure-Python fallbacks so the core ops
  work with no external tools (Windows, minimal containers) (e.g. `fd → git ls-files → find`, `rg → git grep → grep`,
  `hyperfine → builtin`). Each one documents its semantic differences.
- **9 workflow templates**: repo intelligence, disk usage, search & replace, data profiling, system diagnostics,
  network path, benchmarking, git review and archives.
- **Safety**: approval gates for writes, network probes and running code; resource limits (and optional bubblewrap
  isolation) for user code; path locks so concurrent runs can't write the same files; no `rm` fallback; scope
  check for port scans; no privilege escalation.
- **Composition**: "find X then do Y to each of them" becomes a chained plan, with paths wired between steps and per-item `foreach`.
- **Remote targets**: read-only ops over `ssh:HOST` / `docker:NAME` with remote tool detection.
- **Git writes** (commit, branch, absorb) behind gates, with undo that never discards work.
- **Adaptive**: learns which tools fail persistently on this machine, resumes plans from verified steps, extracts
  constraints from plain language ("python files bigger than 1MB changed in the last 2 days"), and suggests how to
  install a better tool using the package managers you actually have.
- **Undo**: write steps snapshot what they touch; `rth rollback <run>` restores it, all-or-nothing, without deleting anything.
- **Optional front ends, opt-in only**: an MCP server (`rth mcp`) and a standalone offline agent (`rth agent`), which
  uses a local LLM such as Qwen Coder only with `--llm`.
- Pure Python 3.9+ stdlib (the registry builder needs 3.11 for `tomllib`). No daemon, database or install step.

## Install

```sh
scripts/install.sh            # symlink into ~/.claude/skills and ~/.codex/skills
scripts/install.sh --copy     # copy instead of symlink; --codex-dir/--claude-dir override paths; --check; --uninstall
scripts/install.sh claude     # Claude Code only
scripts/install.sh codex      # Codex only (uses $CODEX_HOME if set)
scripts/install.sh project /path/to/repo   # Claude Code project skill (.claude/skills)
```
Both agents read the same `SKILL.md`. If your Codex version uses a different skills directory, link the folder there.

## Optional front ends

The harness core needs **no LLM and no MCP**: an agent like Claude Code or Codex drives `bin/rth` through `SKILL.md`.
Two front ends are available and start only when you invoke them:

```sh
bin/rth mcp                                      # stdio MCP server (same as bin/rth-mcp)
bin/rth agent "why is this machine slow"         # standalone agent: deterministic planner, no model, no MCP
bin/rth agent "rename 'old_name' to 'new_name' in src" --allow writes
bin/rth agent "<goal>" --llm ollama              # local model chooses tools (default Qwen Coder)
bin/rth agent "<goal>" --llm openai --url http://127.0.0.1:8080   # llama.cpp / LM Studio / vLLM
bin/rth agent "<goal>" --mcp                     # route the agent's tool calls through the bundled MCP server
```

- `--llm` is the only thing that makes the agent talk to a model. The default model is `qwen2.5-coder:7b`, and any installed
  `qwen*coder*` is preferred; `--model` overrides. Qwen's text-form `<tool_call>` output is parsed, and `--tool-mode json`
  helps small models. By default a short "core" tool set is offered, because prompt size dominates CPU inference time.
- Endpoints that aren't local are refused unless `--allow-remote-llm`.
- State-changing tools need `--allow writes|network|exec` or an interactive y/N (`--no-prompt` denies instead). Privileged
  actions are never granted from the loop. The agent appends `rth_verify` results it checked itself, and logs a transcript
  to `$RTH_STATE/agent/`.
- Exit codes: 0 = verified or answered, 3 = denied or needs approval/input, 1 = anything else.
- `scripts/setup-local-llm.sh [--pull]` checks for Ollama and pulls the model. `scripts/ollama-user-service.sh` runs a
  user-level Ollama as a `systemctl --user` service on localhost (no root; `--remove` to undo).
- On CPU, expect minutes per model step. Measured here, on a machine already loaded by other builds: 190–525 s per step for the 7B model.

## MCP server

`bin/rth-mcp` serves the same functions over MCP (stdio). Read-only tools and state-changing `*_approved` /
`rth_rollback_apply` tools are separate, so you can allowlist the first group and keep prompts on the second.

```sh
claude mcp add rth -- python3 "$PWD/bin/rth-mcp"          # Claude Code
```
```toml
# Codex: ~/.codex/config.toml
[mcp_servers.rth]
command = "python3"
args = ["/abs/path/to/skills/rust-tools-harness/bin/rth-mcp"]
```

## Use directly

```sh
H="python3 bin/rth"
$H env                                  # what this machine has
$H discover "find large files"          # capabilities -> ranked tools (installed or not)
$H select "search code for a regex"     # best executable op here
$H run search-content -p pattern='fn main' -p path=src
$H plan "what is using disk space in ./target" -o /tmp/plan.json && $H execute /tmp/plan.json --brief
$H verify <run-id>                      # evidence per step
$H recover --tool fd --stderr "error: unexpected argument '--glob'" --exit 2
$H audit                                # recent runs
$H rollback <run-id> [--apply] [--force] # undo a run's write steps (preview by default)
```

## Layout

```
SKILL.md                    agent instructions (loaded by Claude Code / Codex)
bin/rth                     CLI entry point
lib/rth/                    registry, env, matcher, planner, ops, executor, validate, recover, audit, cli
registry/                   catalogue.json (parsed README), upstream.json (fetched), curated.json (hand-written),
                            capabilities.json (taxonomy), tools.json (built registry)
workflows/*.json            workflow templates
references/                 architecture.md, workflows.md, catalogue.md (generated)
scripts/build_registry.py   parse | fetch | build | docs
bin/rth-mcp                 MCP server (stdio)
bin/rth-agent               standalone agent (= rth agent): planner by default; --llm for a local model, --mcp for MCP
tests/                      test_harness (core), test_parsers (fixtures/ snapshots), test_undo_mcp, test_agent (fake local LLM),
                            test_live (every installed adapter, pinned); python3 -m unittest discover -s tests
```

## Tests and CI

`python3 -m unittest discover -s tests` runs everything. `test_live.py` runs each op's implementations whose binaries are
installed, pinned to that tool, and fails if the harness used something else. Set `RTH_LIVE_REQUIRE=fd,rg,...` to fail
when listed tools are missing, `RTH_LIVE_WRITES=1` to include safe-delete (it moves files into the real trash), and
`RTH_LIVE_REPORT=out.json` for a matrix. `.github/workflows/rust-tools-harness.yml` runs the unit tests on Python 3.9 and 3.12,
then the live tests on Linux and macOS with tools installed by cargo-binstall.

## Refresh the registry

```sh
python3 scripts/build_registry.py parse       # re-read ../../CATALOGUE.md
python3 scripts/build_registry.py fetch       # only new entries; --refresh for all; --crates-only to redo crates.io matching
python3 scripts/build_registry.py build       # tools.json + references/catalogue.md
python3 scripts/build_registry.py verify-bins [--unknown]   # confirm binary names from Cargo.toml build targets (GitHub tree API)
```
New catalogue entries need a `curated.json` record (capabilities, kind, flags…). `build` warns about missing ones, and
the tests fail until each capability exists in `capabilities.json`.
