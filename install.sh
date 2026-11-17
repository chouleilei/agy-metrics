#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
CONFIG_DIR="${GEMINI_ROOT}/config"
PLUGIN_DIR="${CONFIG_DIR}/plugins/agy-metrics"

echo "Installing agy-metrics..."
echo "  AGY_DIR    : ${AGY_DIR}"
echo "  PLUGIN_DIR : ${PLUGIN_DIR}"

mkdir -p "${PLUGIN_DIR}/scripts" "${AGY_DIR}/bin"

cp "${REPO_DIR}/plugin.json" "${PLUGIN_DIR}/plugin.json"
cp "${REPO_DIR}/hooks.json" "${PLUGIN_DIR}/hooks.json"
cp "${REPO_DIR}/scripts/record_timing.py" "${PLUGIN_DIR}/scripts/record_timing.py"
cp "${REPO_DIR}/scripts/agy_metrics_lib.py" "${PLUGIN_DIR}/scripts/agy_metrics_lib.py"
chmod +x "${PLUGIN_DIR}/scripts/record_timing.py"

cp "${REPO_DIR}/scripts/agy_metrics_lib.py" "${AGY_DIR}/agy_metrics_lib.py"
cp "${REPO_DIR}/scripts/statusline.py" "${AGY_DIR}/statusline.py"
cp "${REPO_DIR}/scripts/refresh_quota.py" "${AGY_DIR}/refresh_quota.py"
cp "${REPO_DIR}/scripts/agy-stats" "${AGY_DIR}/bin/agy-stats"
chmod +x "${AGY_DIR}/statusline.py" "${AGY_DIR}/refresh_quota.py" "${AGY_DIR}/bin/agy-stats"

if [[ -w "/usr/local/bin" ]]; then
  ln -sf "${AGY_DIR}/bin/agy-stats" "/usr/local/bin/agy-stats"
  echo "  CLI link   : /usr/local/bin/agy-stats"
else
  mkdir -p "${HOME}/.local/bin"
  ln -sf "${AGY_DIR}/bin/agy-stats" "${HOME}/.local/bin/agy-stats"
  echo "  CLI link   : ${HOME}/.local/bin/agy-stats"
fi

SETTINGS_FILE="${AGY_DIR}/settings.json"
python3 - "${SETTINGS_FILE}" <<'PY'
import json
import shutil
import sys
from pathlib import Path

p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
data = {}
if p.is_file():
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        shutil.copy2(p, p.with_suffix(".json.bak"))
        data = {}

data["statusLine"] = {
    "type": "command",
    "command": "python3 ~/.gemini/antigravity-cli/statusline.py",
    "enabled": True,
}
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if command -v agy >/dev/null 2>&1; then
  agy plugin validate --strict "${PLUGIN_DIR}" || true
fi

echo "Done! Restart agy CLI to see the status bar, and run 'agy-stats' for history."
