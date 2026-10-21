#!/bin/sh
# Run a user-level Ollama (~/.local/bin/ollama) as a systemd --user service bound to localhost.
# No root needed. Starts at login; for start-at-boot without login run (needs root once):
#   sudo loginctl enable-linger "$USER"
#   scripts/ollama-user-service.sh            install + enable + start
#   scripts/ollama-user-service.sh --remove   stop + disable + remove the unit
set -eu
UNIT="$HOME/.config/systemd/user/ollama.service"
BIN=$(command -v ollama || echo "$HOME/.local/bin/ollama")
if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now ollama.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl --user daemon-reload; echo "removed $UNIT"; exit 0
fi
[ -x "$BIN" ] || { echo "ollama not found (expected $BIN)"; exit 1; }
if [ -e "$UNIT" ] && ! grep -q "rust-tools-harness" "$UNIT"; then
  echo "$UNIT exists and was not created by this script; leaving it alone"; exit 1
fi
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNITEOF
# created by rust-tools-harness scripts/ollama-user-service.sh
[Unit]
Description=Ollama (user, localhost only)
After=network-online.target

[Service]
ExecStart=$BIN serve
Environment=OLLAMA_HOST=127.0.0.1:11434
Restart=on-failure
RestartSec=3
Nice=10

[Install]
WantedBy=default.target
UNITEOF
systemctl --user daemon-reload
systemctl --user enable --now ollama.service
for i in $(seq 1 20); do curl -fsS -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
systemctl --user --no-pager --lines=0 status ollama.service | head -3
curl -fsS -m 2 http://127.0.0.1:11434/api/version && echo
[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ] || echo "note: starts at login; for boot without login: sudo loginctl enable-linger $USER"
