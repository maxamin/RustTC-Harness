#!/bin/sh
# Install this skill for Claude Code and/or Codex. Never overwrites anything it did not create.
#
#   scripts/install.sh [targets] [options]
#   targets:  claude  codex  project DIR   (default: claude codex)
#   options:  --copy            copy files instead of symlinking (e.g. no symlink support, or to pin a version)
#             --codex-dir DIR   Codex skills directory (default ${CODEX_HOME:-~/.codex}/skills)
#             --claude-dir DIR  Claude Code skills directory (default ~/.claude/skills)
#             --check           only report what is installed where
#             --uninstall       remove links/copies this script created (marker-checked)
#
# The Codex default is an assumption about current Codex releases; if your Codex reads skills from
# another directory, pass --codex-dir. Both agents read the same SKILL.md.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
NAME=rust-tools-harness
MARK=.rth-installed-copy
MODE=link; ACTION=install; TARGETS=""
CLAUDE_DIR="$HOME/.claude/skills"; CODEX_DIR="${CODEX_HOME:-$HOME/.codex}/skills"; PROJECT=""

while [ $# -gt 0 ]; do
  case "$1" in
    claude|codex) TARGETS="$TARGETS $1" ;;
    project) PROJECT="${2:?project needs a DIR}"; TARGETS="$TARGETS project"; shift ;;
    --copy) MODE=copy ;;
    --codex-dir) CODEX_DIR="${2:?}"; shift ;;
    --claude-dir) CLAUDE_DIR="${2:?}"; shift ;;
    --check) ACTION=check ;;
    --uninstall) ACTION=uninstall ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$TARGETS" ] || TARGETS="claude codex"

dir_for() {
  case "$1" in
    claude) echo "$CLAUDE_DIR" ;;
    codex) echo "$CODEX_DIR" ;;
    project) echo "$PROJECT/.claude/skills" ;;
  esac
}

ours() {  # is $1 something this script created?
  { [ -L "$1" ] && [ "$(readlink "$1")" = "$SRC" ]; } || [ -f "$1/$MARK" ]
}

for t in $TARGETS; do
  base=$(dir_for "$t"); dest="$base/$NAME"
  case "$ACTION" in
    check)
      if ours "$dest"; then echo "$t: installed at $dest ($( [ -L "$dest" ] && echo link || echo copy ))"
      elif [ -e "$dest" ]; then echo "$t: $dest exists but was not created by this installer"
      else echo "$t: not installed ($dest)"; fi ;;
    uninstall)
      if ours "$dest"; then
        if [ -L "$dest" ]; then rm "$dest"; else rm -r "$dest"; fi
        echo "$t: removed $dest"
      elif [ -e "$dest" ]; then echo "$t: left $dest alone (not created by this installer)"
      else echo "$t: nothing to remove"; fi ;;
    install)
      mkdir -p "$base"
      if [ -e "$dest" ] || [ -L "$dest" ]; then
        if ours "$dest"; then
          if [ "$MODE" = copy ] && [ -f "$dest/$MARK" ]; then rm -r "$dest"   # refresh our own copy
          elif [ "$MODE" = link ] && [ -L "$dest" ]; then echo "$t: already linked: $dest"; continue
          else echo "$t: $dest is a $( [ -L "$dest" ] && echo link || echo copy ) from this installer; --uninstall first to switch mode"; continue; fi
        else
          echo "$t: skip, $dest exists and was not created by this installer"; continue
        fi
      fi
      if [ "$MODE" = copy ]; then
        mkdir -p "$dest"
        (cd "$SRC" && tar --exclude=__pycache__ -cf - .) | (cd "$dest" && tar -xf -)
        echo "$SRC" > "$dest/$MARK"
        echo "$t: copied to $dest"
      else
        ln -s "$SRC" "$dest" && echo "$t: linked $dest -> $SRC"
      fi ;;
  esac
done
if [ "$ACTION" = install ]; then
  python3 "$SRC/bin/rth" doctor >/dev/null && echo "rth doctor: ok"
  command -v codex >/dev/null 2>&1 || case "$TARGETS" in *codex*) echo "note: codex not found on PATH; installed to $CODEX_DIR anyway (pass --codex-dir if your Codex uses another path)";; esac
fi
