#!/bin/sh
# Prepare the offline agent's local model: Qwen2.5-Coder via Ollama.
#   scripts/setup-local-llm.sh            # check only: is Ollama installed/running, is the model pulled?
#   scripts/setup-local-llm.sh --pull     # also `ollama pull` the model (~4.7 GB for 7b)
# Env: RTH_AGENT_MODEL (default qwen2.5-coder:7b; e.g. qwen2.5-coder:14b, qwen2.5-coder:32b, qwen3-coder:30b)
# Installing Ollama itself is left to you (it needs root): see https://ollama.com/download
set -eu
MODEL=${RTH_AGENT_MODEL:-qwen2.5-coder:7b}
URL=${OLLAMA_HOST:-http://127.0.0.1:11434}
case "$URL" in http*) ;; *) URL="http://$URL" ;; esac

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama: not installed."
  echo "  Linux: curl -fsSL https://ollama.com/install.sh | sh    (review the script first; needs sudo)"
  echo "  macOS: brew install ollama   |  or download from https://ollama.com/download"
  echo "The harness and rth agent work without it (planner mode); only --llm ollama needs it."
  exit 1
fi
echo "ollama: $(ollama --version 2>/dev/null | head -1)"
if ! curl -fsS -m 3 "$URL/api/tags" >/dev/null 2>&1; then
  echo "server: not reachable at $URL - start it with: ollama serve   (or: systemctl start ollama)"
  exit 1
fi
if curl -fsS -m 3 "$URL/api/tags" | grep -q "\"$MODEL\""; then
  echo "model: $MODEL present"
elif [ "${1:-}" = "--pull" ]; then
  ollama pull "$MODEL"
else
  echo "model: $MODEL not pulled - rerun with --pull (or: ollama pull $MODEL)"
  exit 1
fi
echo "ready: bin/rth agent \"<goal>\" --llm ollama --model $MODEL"
