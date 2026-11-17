#!/usr/bin/env python3
"""Custom agy CLI statusLine script.

Displays: Context % (512K) | Session (X轮Y步) In/Out | Cache % | Speed | Gemini Weekly % | Cost
"""

import fcntl
import json
import os
import subprocess
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
    calc_cache_rate,
    calc_step_cost_usd,
    find_transcript_path,
    fmt_tokens,
    fmt_usd,
    load_json_safe,
    read_subagent_aggregates,
    read_transcript_incremental,
    safe_id,
    session_lock,
)

RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"

MAX_CTX_CAP = int(os.environ.get("AGY_MAX_CTX_TOKENS", 512 * 1024))
QUOTA_TTL_SEC = int(os.environ.get("AGY_QUOTA_TTL_SEC", 180))
QUOTA_RETRY_COOLDOWN_SEC = 60
QUOTA_CACHE_FILE = CACHE_DIR / "quota_cache.json"
QUOTA_LOCK_FILE = CACHE_DIR / "quota_refresh.lock"
REFRESH_SCRIPT = _SCRIPT_DIR / "refresh_quota.py"


def get_gemini_weekly_pct() -> float | None:
    """Read cached Gemini weekly quota % and trigger non-blocking refresh when stale."""
    cached = load_json_safe(QUOTA_CACHE_FILE) if QUOTA_CACHE_FILE.is_file() else {}
    now = time.time()
    fetched_at = float(cached.get("fetched_at") or 0.0)
    last_attempt = float(cached.get("last_attempt") or 0.0)

    is_stale = (now - fetched_at) >= QUOTA_TTL_SEC
    cooldown_ok = (now - last_attempt) >= QUOTA_RETRY_COOLDOWN_SEC

    if (
        is_stale
        and cooldown_ok
        and os.environ.get("AGY_NO_QUOTA_REFRESH") != "1"
        and REFRESH_SCRIPT.is_file()
    ):
        lock_f = None
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            lock_f = QUOTA_LOCK_FILE.open("a+", encoding="utf-8")
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            cached = load_json_safe(QUOTA_CACHE_FILE) if QUOTA_CACHE_FILE.is_file() else {}
            if (
                (now - float(cached.get("fetched_at") or 0.0)) >= QUOTA_TTL_SEC
                and (now - float(cached.get("last_attempt") or 0.0)) >= QUOTA_RETRY_COOLDOWN_SEC
            ):
                cached["last_attempt"] = now
                atomic_write_json(QUOTA_CACHE_FILE, cached)
                subprocess.Popen(
                    [sys.executable, str(REFRESH_SCRIPT)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception:
            pass
        finally:
            if lock_f is not None:
                try:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                lock_f.close()

    val = cached.get("gemini_weekly_pct")
    return float(val) if isinstance(val, (int, float)) else None


def color_quota_pct(pct: float) -> str:
    if pct <= 15:
        return f"{RED}{pct:.1f}%{RESET}"
    if pct <= 30:
        return f"{YELLOW}{pct:.1f}%{RESET}"
    return f"{GREEN}{pct:.1f}%{RESET}"


def calc_effective_ctx_pct(ctx: dict, cur: dict, fallback_ctx_tokens: int = 0) -> float:
    """Recalculate context percentage against actual max window (default 512K)."""
    used_pct = float(ctx.get("used_percentage") or 0.0)
    official_win = int(ctx.get("context_window_size") or 0)
    if official_win > 0:
        effective_win = min(official_win, MAX_CTX_CAP)
        if effective_win < official_win:
            if used_pct > 0:
                return used_pct * (official_win / effective_win)
            if isinstance(cur, dict) and cur:
                cur_total = (
                    int(cur.get("input_tokens") or 0)
                    + int(cur.get("cache_read_input_tokens") or 0)
                    + int(cur.get("cache_creation_input_tokens") or 0)
                    + int(cur.get("output_tokens") or 0)
                )
                return (cur_total / effective_win) * 100.0
            if fallback_ctx_tokens > 0:
                return (fallback_ctx_tokens / effective_win) * 100.0
        elif used_pct == 0 and fallback_ctx_tokens > 0 and not cur:
            return (fallback_ctx_tokens / effective_win) * 100.0
        return used_pct

    if isinstance(cur, dict) and cur:
        cur_total = (
            int(cur.get("input_tokens") or 0)
            + int(cur.get("cache_read_input_tokens") or 0)
            + int(cur.get("cache_creation_input_tokens") or 0)
            + int(cur.get("output_tokens") or 0)
        )
        if cur_total > 0:
            return (cur_total / MAX_CTX_CAP) * 100.0
    if fallback_ctx_tokens > 0 and used_pct == 0:
        return (fallback_ctx_tokens / MAX_CTX_CAP) * 100.0
    return used_pct


def color_pct(pct: float) -> str:
    if pct >= 80:
        return f"{RED}{pct:.1f}%{RESET}"
    if pct >= 55:
        return f"{YELLOW}{pct:.1f}%{RESET}"
    return f"{GREEN}{pct:.1f}%{RESET}"


def extract_model_id(data: dict, t_stats: dict) -> str:
    m = t_stats.get("last_model")
    if m:
        return str(m)
    dm = data.get("model")
    if isinstance(dm, dict):
        return dm.get("id") or dm.get("display_name") or "unknown"
    if isinstance(dm, str) and dm:
        return dm
    return "unknown"


def capture_live_timing(session_id: str, sess_out: int, model_key: str) -> dict:
    if not session_id:
        return {}
    state_path = CACHE_DIR / f"{safe_id(session_id)}.json"
    with session_lock(session_id):
        timing = load_json_safe(state_path)
        now = time.time()
        if (
            timing.get("inv_active")
            and timing.get("inv_start")
            and not timing.get("inv_captured")
            and sess_out > 0
        ):
            start_out = int(timing.get("inv_start_out") or 0)
            delta_out = sess_out - start_out
            if delta_out > 0:
                elapsed = max(0.05, now - float(timing["inv_start"]))
                cum_out = int(timing.get("cum_out") or 0) + delta_out
                cum_elapsed = float(timing.get("cum_elapsed") or 0.0) + elapsed

                by_model = timing.setdefault("by_model", {})
                mb = by_model.setdefault(model_key or "unknown", {"out": 0, "elapsed": 0.0})
                mb["out"] = int(mb.get("out") or 0) + delta_out
                mb["elapsed"] = round(float(mb.get("elapsed") or 0.0) + elapsed, 3)

                timing["last_elapsed"] = round(elapsed, 3)
                timing["last_out_tokens"] = delta_out
                timing["last_tps"] = round(delta_out / elapsed, 2)
                timing["cum_out"] = cum_out
                timing["cum_elapsed"] = round(cum_elapsed, 3)
                timing["cum_tps"] = round(cum_out / cum_elapsed, 2)
                timing["inv_captured"] = True
                timing["last_updated"] = now
                atomic_write_json(state_path, timing)
        return timing


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("agy-metrics")
        return

    session_id = data.get("session_id") or data.get("conversation_id") or ""
    ctx = data.get("context_window") or {}
    total_in = int(ctx.get("total_input_tokens") or 0)
    total_out = int(ctx.get("total_output_tokens") or 0)
    cur = ctx.get("current_usage") or {}

    t_path = find_transcript_path(session_id, data.get("transcript_path") or "")
    if t_path and session_id:
        with session_lock(session_id):
            t_stats = read_transcript_incremental(t_path, session_id)
    else:
        t_stats = read_transcript_incremental(t_path, session_id) if t_path else {}

    used_pct = calc_effective_ctx_pct(ctx, cur, int(t_stats.get("last_ctx_tokens") or 0))

    user_turns = int(t_stats.get("user_turns") or 0)
    llm_steps = int(t_stats.get("turns") or 0)
    sess_in_uncached = int(t_stats.get("input_tokens") or 0)
    sess_cache_read = int(t_stats.get("cache_read_tokens") or 0)
    sess_out = int(t_stats.get("output_tokens") or 0)

    sub_agg = read_subagent_aggregates(session_id) if session_id else {}
    sub_count = int(sub_agg.get("sub_count") or 0)
    agg_steps = llm_steps + int(sub_agg.get("turns") or 0)
    agg_in_uncached = sess_in_uncached + int(sub_agg.get("input_tokens") or 0)
    agg_cache_read = sess_cache_read + int(sub_agg.get("cache_read_tokens") or 0)
    agg_out = sess_out + int(sub_agg.get("output_tokens") or 0)
    sub_cost_usd = float(sub_agg.get("est_cost_usd") or 0.0)

    has_local = bool(t_stats) or sub_count > 0
    display_in = (agg_in_uncached + agg_cache_read) if has_local else total_in
    display_out = agg_out if (has_local and agg_out > 0) else total_out

    sess_cache_pct = calc_cache_rate(agg_in_uncached, agg_cache_read) if has_local else None
    cur_cache_pct = None
    if isinstance(cur, dict) and cur:
        cur_cache_pct = calc_cache_rate(
            int(cur.get("input_tokens") or 0),
            int(cur.get("cache_read_input_tokens") or 0),
            int(cur.get("cache_creation_input_tokens") or 0),
        )

    model_key = extract_model_id(data, t_stats)
    timing = capture_live_timing(session_id, sess_out, model_key)

    parts = [f"Ctx {color_pct(used_pct)}"]

    sub_tag = f"+{sub_count}子代理" if sub_count > 0 else ""
    turn_step_tag = f"({user_turns}轮{agg_steps}步{sub_tag})" if (user_turns or agg_steps) else ""
    parts.append(
        f"会话{turn_step_tag} {BLUE}In:{fmt_tokens(display_in)}{RESET} {MAGENTA}Out:{fmt_tokens(display_out)}{RESET}"
    )

    if sess_cache_pct is not None and cur_cache_pct is not None:
        parts.append(f"缓存 {GREEN}{sess_cache_pct:.1f}%{RESET}{DIM}(本轮{cur_cache_pct:.0f}%){RESET}")
    elif sess_cache_pct is not None:
        parts.append(f"缓存 {GREEN}{sess_cache_pct:.1f}%{RESET}")
    elif cur_cache_pct is not None:
        parts.append(f"缓存 {GREEN}{cur_cache_pct:.1f}%{RESET}")
    else:
        parts.append(f"缓存 {DIM}--{RESET}")

    tps = timing.get("last_tps")
    cum_tps = timing.get("cum_tps")
    elapsed = timing.get("last_elapsed")
    if tps is not None and cum_tps is not None:
        parts.append(
            f"速度 {YELLOW}{tps:.1f}{RESET}{DIM}({elapsed:.1f}s){RESET}/平均{YELLOW}{cum_tps:.1f}{RESET} tok/s"
        )
    elif tps is not None:
        parts.append(f"速度 {YELLOW}{tps:.1f} tok/s{RESET}{DIM}({elapsed:.1f}s){RESET}")
    else:
        parts.append(f"速度 {DIM}--{RESET}")

    weekly_pct = get_gemini_weekly_pct()
    if weekly_pct is not None:
        parts.append(f"周余 {color_quota_pct(weekly_pct)}")
    else:
        parts.append(f"周余 {DIM}--{RESET}")

    cost = data.get("cost") or {}
    official_usd = None
    if isinstance(cost, dict):
        official_usd = cost.get("total_usd")
        if not (isinstance(official_usd, (int, float)) and official_usd > 0):
            official_usd = cost.get("total_cost_usd")

    if isinstance(official_usd, (int, float)) and official_usd > 0:
        display_usd = float(official_usd) + sub_cost_usd
    else:
        display_usd = (float(t_stats.get("est_cost_usd") or 0.0) if t_stats else 0.0) + sub_cost_usd
        if display_usd <= 0 and isinstance(cur, dict) and cur:
            display_usd = calc_step_cost_usd(
                model_key,
                int(cur.get("input_tokens") or 0),
                int(cur.get("cache_read_input_tokens") or 0),
                int(cur.get("output_tokens") or 0),
            )

    if display_usd > 0:
        usd_cny = os.environ.get("AGY_USD_CNY", "").strip()
        if usd_cny:
            try:
                cny = display_usd * float(usd_cny)
                parts.append(f"{DIM}~¥{cny:.2f}{RESET}")
            except ValueError:
                parts.append(f"{DIM}~{fmt_usd(display_usd)}{RESET}")
        else:
            parts.append(f"{DIM}~{fmt_usd(display_usd)}{RESET}")

    sep = f" {DIM}│{RESET} "
    print(sep.join(parts))


if __name__ == "__main__":
    main()
