#!/usr/bin/env python3
"""Background quota refresher for agy CLI statusLine.

Calls official `agy --print /usage --output-format json` in a single-instance
background process and caches Gemini weekly remaining percentage locally.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if (_SCRIPT_DIR / "agy_metrics_lib.py").is_file():
    sys.path.insert(0, str(_SCRIPT_DIR))
else:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from agy_metrics_lib import CACHE_DIR, atomic_write_json, load_json_safe

QUOTA_CACHE_FILE = CACHE_DIR / "quota_cache.json"
QUOTA_LOCK_FILE = CACHE_DIR / "quota_refresh.lock"


def find_agy_bin() -> str:
    for candidate in ("/usr/local/bin/agy", "/data/antigravity/bin/agy"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("agy") or "agy"


def extract_gemini_weekly(data: dict) -> dict | None:
    cmd = data.get("command") or {}
    groups = (cmd.get("data") or {}).get("groups") or []
    for g in groups:
        name = (g.get("name") or "").lower()
        if "gemini" in name:
            for b in g.get("buckets") or []:
                bid = (b.get("id") or "").lower()
                win = (b.get("window") or "").lower()
                if bid == "gemini-weekly" or win == "weekly":
                    frac = b.get("remaining_fraction")
                    if isinstance(frac, (int, float)):
                        return {
                            "gemini_weekly_pct": round(float(frac) * 100.0, 1),
                            "reset_time": b.get("reset_time") or "",
                        }
    return None


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_f = QUOTA_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_f.close()
        return

    try:
        existing = load_json_safe(QUOTA_CACHE_FILE)
        now = time.time()
        existing["last_attempt"] = now
        atomic_write_json(QUOTA_CACHE_FILE, existing)

        env = dict(os.environ)
        env["AGY_NO_QUOTA_REFRESH"] = "1"
        agy_bin = find_agy_bin()
        proc = subprocess.run(
            [agy_bin, "--print", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = None
            for line in reversed(proc.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except Exception:
                        continue
            if isinstance(parsed, dict):
                info = extract_gemini_weekly(parsed)
                if info:
                    existing.update(info)
                    existing["fetched_at"] = time.time()
                    atomic_write_json(QUOTA_CACHE_FILE, existing)
    except Exception:
        pass
    finally:
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        finally:
            lock_f.close()


if __name__ == "__main__":
    main()
