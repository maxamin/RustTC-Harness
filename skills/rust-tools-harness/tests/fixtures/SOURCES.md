# Parser fixtures

Recorded from real runs (Linux, 2026-10-21): rg 14.1.1 (`rg.jsonl`, `rg_replace.txt`), fd 10.5.0 (`fd.txt`),
git (`git_status_v2.txt`, `git_numstat.txt`, `git_grep.txt`), tokei 12.1.2 (`tokei.json`), GNU du (`du.txt`),
hyperfine 1.19.0 (`hyperfine.json`), jaq 3.1.1 (`jaq.txt`).

Sanitized, format-identical to real output (machine details removed): `df.txt` (GNU `df -kP`), `ps_linux.txt` (procps `ps -eo ...`).

Derived from upstream source, not yet recorded from a binary (the live tests in CI cover these):
`diskus.txt` (format string `"{} ({:} bytes)"` in diskus src/main.rs), `dust_bytes.json` (`dust -j -o b`,
DisplayNode serializer in dust src/display_node.rs).
