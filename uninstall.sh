#!/usr/bin/env bash
set -euo pipefail

detect_gemini_root() {
  if [[ -n "${AGY_DATA_DIR:-}" ]]; then
    dirname "$AGY_DATA_DIR"
    return
  fi
  if [[ -d "${HOME}/.gemini/antigravity-cli" ]]; then
    echo "${HOME}/.gemini"
    return
  fi
  if [[ -d "/data/antigravity/home/.gemini/antigravity-cli" ]]; then
    echo "/data/antigravity/home/.gemini"
    return
  fi
  echo "${HOME}/.gemini"
}

GEMINI_ROOT="$(detect_gemini_root)"
AGY_DIR="${AGY_DATA_DIR:-${GEMINI_ROOT}/antigravity-cli}"
PLUGIN_DIR="${GEMINI_ROOT}/config/plugins/agy-metrics"
SETTINGS_FILE="${AGY_DIR}/settings.json"

echo "Uninstalling agy-metrics..."

if [[ -f "${SETTINGS_FILE}" ]]; then
  python3 - "${SETTINGS_FILE}" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

if isinstance(data.get("statusLine"), dict):
    data["statusLine"] = {"type": "", "command": "", "enabled": False}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

rm -rf "${PLUGIN_DIR}"
rm -rf "${AGY_DIR}/cache/agy_metrics"
rm -f "${AGY_DIR}/statusline.py" "${AGY_DIR}/refresh_quota.py" "${AGY_DIR}/agy_metrics_lib.py" "${AGY_DIR}/bin/agy-stats"

for link in "/usr/local/bin/agy-stats" "${HOME}/.local/bin/agy-stats"; do
  if [[ -L "${link}" ]]; then
    rm -f "${link}"
  fi
done

echo "Uninstalled agy-metrics. Your conversation logs were not modified."
