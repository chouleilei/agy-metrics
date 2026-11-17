#!/usr/bin/env python3
"""Local-only lifecycle timing hook for agy-metrics.

Tracks per-invocation timing (for latest step & cumulative session effective Token/s)
using advisory file locks and local transcript data without any network I/O.
"""

import json
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if (_SCRIPT_DIR / "agy_metrics_lib.py").is_file():
    sys.path.insert(0, str(_SCRIPT_DIR))
else:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from agy_metrics_lib import (
    CACHE_DIR,
    atomic_write_json,
    find_transcript_path,
    load_json_safe,
    read_transcript_incremental,
    safe_id,
    session_lock,
)


def read_transcript_info(transcript_path: str, conv_id: str) -> tuple[int, str]:
    """Return (total_output_tokens, last_model) from transcript.jsonl."""
    p = find_transcript_path(conv_id, transcript_path)
    if p is None or not p.is_file():
        return 0, ""
    stats = read_transcript_incremental(p, conv_id)
    return int(stats.get("output_tokens") or 0), str(stats.get("last_model") or "")


def main() -> None:
    event = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = payload.get("hookEventName", "")
        conv_id = payload.get("conversationId", "")
        transcript_path = payload.get("transcriptPath", "")

        if conv_id:
            with session_lock(conv_id):
                state_path = CACHE_DIR / f"{safe_id(conv_id)}.json"
                state = load_json_safe(state_path)
                now = time.time()

                if transcript_path:
                    state["transcript_path"] = transcript_path

                if event == "SessionStart":
                    state.setdefault("session_start", now)
                elif event == "PreInvocation":
                    total_out, _ = read_transcript_info(transcript_path, conv_id)
                    state["inv_start"] = now
                    state["inv_num"] = payload.get("invocationNum", 0)
                    state["inv_start_out"] = total_out
                    state["inv_active"] = True
                    state["inv_captured"] = False
                elif event in ("PostInvocation", "Stop"):
                    inv_start = state.get("inv_start")
                    already_captured = bool(state.get("inv_captured"))
                    if inv_start and now > float(inv_start) and not already_captured:
                        total_out, last_model = read_transcript_info(transcript_path, conv_id)
                        start_out = int(state.get("inv_start_out") or 0)
                        delta_out = total_out - start_out
                        if delta_out > 0:
                            elapsed = max(0.05, now - float(inv_start))
                            cum_out = int(state.get("cum_out") or 0) + delta_out
                            cum_elapsed = float(state.get("cum_elapsed") or 0.0) + elapsed

                            model_key = last_model or payload.get("modelName") or "unknown"
                            by_model = state.setdefault("by_model", {})
                            mb = by_model.setdefault(model_key, {"out": 0, "elapsed": 0.0})
                            mb["out"] = int(mb.get("out") or 0) + delta_out
                            mb["elapsed"] = round(float(mb.get("elapsed") or 0.0) + elapsed, 3)

                            state["last_elapsed"] = round(elapsed, 3)
                            state["last_out_tokens"] = delta_out
                            state["last_tps"] = round(delta_out / elapsed, 2)
                            state["cum_out"] = cum_out
                            state["cum_elapsed"] = round(cum_elapsed, 3)
                            state["cum_tps"] = round(cum_out / cum_elapsed, 2)
                            state["inv_captured"] = True
                            state["last_updated"] = now
                    if event == "Stop":
                        state["inv_active"] = False

                atomic_write_json(state_path, state)
    except Exception:
        pass

    if event == "Stop":
        sys.stdout.write('{"decision": "allow"}\n')
    else:
        sys.stdout.write("{}\n")


if __name__ == "__main__":
    main()
