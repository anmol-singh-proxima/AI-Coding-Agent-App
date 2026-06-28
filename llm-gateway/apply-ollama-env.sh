#!/usr/bin/env bash
#
# Apply the Ollama tuning settings from ollama.env to the running Ollama.app.
#
# macOS GUI apps inherit environment variables from the user's launchd session,
# so we register each setting with `launchctl setenv` and then restart Ollama so
# it picks them up. Re-run this script any time you edit ollama.env.
#
# Note: launchctl setenv values persist until you log out / reboot. After a
# reboot, re-run this script (or add it to your Login Items) to re-apply them.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/ollama.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: $ENV_FILE not found" >&2
  exit 1
fi

echo "Applying Ollama settings from ollama.env:"

# Read KEY=VALUE lines, skipping blanks and comments.
while IFS= read -r line || [[ -n "$line" ]]; do
  # Strip leading/trailing whitespace.
  line="${line#"${line%%[![:space:]]*}"}"
  [[ -z "$line" || "$line" == \#* ]] && continue

  key="${line%%=*}"
  value="${line#*=}"
  # Trim surrounding whitespace from key/value.
  key="$(echo "$key" | xargs)"
  value="$(echo "$value" | xargs)"
  [[ -z "$key" ]] && continue

  launchctl setenv "$key" "$value"
  printf '  %-26s = %s\n' "$key" "$value"
done < "$ENV_FILE"

echo
echo "Restarting Ollama so it picks up the new environment..."
# A graceful GUI quit alone does NOT reliably stop the `ollama serve`
# subprocess, and `open` then sees it "already running" and skips the fresh
# launch — so the new env never propagates. We therefore tear down both the GUI
# app and the serve subprocess explicitly before relaunching.
osascript -e 'quit app "Ollama"' 2>/dev/null || true
sleep 1
pkill -x "Ollama" 2>/dev/null || true
pkill -f "Resources/ollama serve" 2>/dev/null || true

# Wait for both to fully exit.
for _ in {1..20}; do
  pgrep -f ollama >/dev/null || break
  sleep 0.5
done

open -a Ollama

# Wait for the server to accept requests again.
for _ in {1..30}; do
  ollama ps >/dev/null 2>&1 && break
  sleep 1
done

echo "Done. Verify with:  ollama ps"
echo "(After a request, CONTEXT and UNTIL should match your ollama.env values.)"
