"""Shared helpers for agy-metrics hooks, statusLine, and agy-stats CLI."""

import fcntl
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


def resolve_agy_dir() -> Path:
    env_dir = os.environ.get("AGY_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    home_agy = Path.home() / ".gemini" / "antigravity-cli"
    if home_agy.is_dir():
        return home_agy
    wrapper_agy = Path("/data/antigravity/home/.gemini/antigravity-cli")
    if wrapper_agy.is_dir():
        return wrapper_agy
    return home_agy


AGY_DIR = resolve_agy_dir()
BRAIN_DIR = AGY_DIR / "brain"
CACHE_DIR = AGY_DIR / "cache" / "agy_metrics"
SUMMARIES_DB = AGY_DIR / "conversation_summaries.db"
PRICING_FILE = AGY_DIR / "pricing.json"
TCACHE_VERSION = 6

_CUSTOM_PRICING_CACHE: dict | None = None


def _load_custom_pricing() -> dict:
    global _CUSTOM_PRICING_CACHE
    if _CUSTOM_PRICING_CACHE is not None:
        return _CUSTOM_PRICING_CACHE
    data = {}
    if PRICING_FILE.is_file():
        try:
            raw = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            pass
    _CUSTOM_PRICING_CACHE = data
    return data


def get_model_pricing(model: str, ts_iso: str = "") -> tuple[float, float, float] | None:
    """Return (input, cache_read, output) USD per 1M tokens, or None if unknown."""
    m = (model or "").lower()
    if not m:
        return None

    custom = _load_custom_pricing()
    if custom:
        if m in custom and isinstance(custom[m], dict):
            c = custom[m]
            return float(c.get("input", 0)), float(c.get("cache_read", 0)), float(c.get("output", 0))
        for prefix in sorted(custom.keys(), key=len, reverse=True):
            if m.startswith(prefix.lower()) and isinstance(custom[prefix], dict):
                c = custom[prefix]
                return float(c.get("input", 0)), float(c.get("cache_read", 0)), float(c.get("output", 0))

    day_str = ts_iso[:10] if len(ts_iso) >= 10 else datetime.now().strftime("%Y-%m-%d")

    if m.startswith("gemini-4-hydrogen"):
        return 1.50, 0.075, 7.50
    if m.startswith("gemini-4-carbon"):
        return (4.00, 0.20, 20.00) if day_str >= "2026-12-01" else (2.00, 0.20, 10.00)
    if m.startswith("gemini-4-argon"):
        return (4.00, 0.20, 20.00) if day_str >= "2026-12-01" else (2.00, 0.10, 10.00)
    if m.startswith("gemini-3.8-flash"):
        return (1.50, 0.075, 7.50) if day_str >= "2027-01-01" else (0.75, 0.0375, 3.75)
    if m.startswith("claude-opus-5-5"):
        return 4.00, 0.20, 20.00
    if m.startswith("claude-sonnet-5-5"):
        return 2.00, 0.10, 10.00
    return None


def calc_step_cost_usd(
    model: str,
    uncached_input: int,
    cache_read: int,
    output: int,
    ts_iso: str = "",
) -> float:
    rates = get_model_pricing(model, ts_iso)
    if not rates:
        return 0.0
    p_in, p_cr, p_out = rates
    return (uncached_input * p_in + cache_read * p_cr + output * p_out) / 1_000_000.0


def fmt_usd(amount: float) -> str:
    if amount <= 0:
        return "--"
    if amount < 0.1:
        return f"${amount:.3f}"
    return f"${amount:.2f}"


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    total_sec = int(round(seconds))
    mins, secs = divmod(total_sec, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def merge_intervals_duration(intervals: list[list[float]] | list[tuple[float, float]]) -> float:
    """Return total wall-clock duration covered by possibly overlapping [start, end] intervals."""
    valid = []
    for item in intervals or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        s, e = float(item[0]), float(item[1])
        if e > s:
            valid.append((s, e))
    if not valid:
        return 0.0
    valid.sort(key=lambda x: x[0])
    total = 0.0
    cur_s, cur_e = valid[0]
    for s, e in valid[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def heal_timing_state(state: dict) -> bool:
    """Backfill legacy cum_model_elapsed from cum_elapsed so cum_tps stays consistent across upgrades."""
    if not isinstance(state, dict):
        return False
    cum_el = float(state.get("cum_elapsed") or 0.0)
    cum_out = int(state.get("cum_out") or 0)
    if cum_el <= 0:
        return False
    m_raw = state.get("cum_model_elapsed")
    t_raw = state.get("cum_tool_elapsed")
    if m_raw is None and t_raw is None:
        return False
    m_el = float(m_raw or 0.0)
    t_el = float(t_raw or 0.0)
    gap = cum_el - (m_el + t_el)
    if gap > 1.0:
        m_el += gap
        state["cum_model_elapsed"] = round(m_el, 3)
        state["cum_tool_elapsed"] = round(t_el, 3)
        if cum_out > 0 and m_el > 0:
            state["cum_tps"] = round(cum_out / m_el, 2)
        return True
    return False


def finalize_invocation_timing(state: dict, now: float, delta_out: int, model_key: str) -> bool:
    """Finalize one invocation's model/tool durations and pure-model TPS into state."""
    inv_start = state.get("inv_start")
    if not inv_start or now <= float(inv_start) or state.get("inv_captured") or delta_out <= 0:
        return False

    heal_timing_state(state)

    total_elapsed = max(0.05, now - float(inv_start))
    intervals = list(state.get("inv_tool_intervals") or [])
    active_tools = state.get("inv_active_tools") or {}
    for st in active_tools.values():
        st_f = float(st)
        intervals.append([st_f, max(st_f, now)])

    tool_elapsed = min(total_elapsed, merge_intervals_duration(intervals))
    model_elapsed = max(0.05, total_elapsed - tool_elapsed)

    prev_cum_el = float(state.get("cum_elapsed") or 0.0)
    prev_m_el = state.get("cum_model_elapsed")
    base_m_el = float(prev_m_el) if prev_m_el is not None else prev_cum_el

    cum_out = int(state.get("cum_out") or 0) + delta_out
    cum_elapsed = prev_cum_el + total_elapsed
    cum_model_elapsed = base_m_el + model_elapsed
    cum_tool_elapsed = float(state.get("cum_tool_elapsed") or 0.0) + tool_elapsed

    by_model = state.setdefault("by_model", {})
    mb = by_model.setdefault(model_key or "unknown", {"out": 0, "elapsed": 0.0})
    mb["out"] = int(mb.get("out") or 0) + delta_out
    mb["elapsed"] = round(float(mb.get("elapsed") or 0.0) + model_elapsed, 3)

    state["last_elapsed"] = round(model_elapsed, 3)
    state["last_tool_elapsed"] = round(tool_elapsed, 3)
    state["last_out_tokens"] = delta_out
    state["last_tps"] = round(delta_out / model_elapsed, 2)
    state["cum_out"] = cum_out
    state["cum_elapsed"] = round(cum_elapsed, 3)
    state["cum_model_elapsed"] = round(cum_model_elapsed, 3)
    state["cum_tool_elapsed"] = round(cum_tool_elapsed, 3)
    state["cum_tps"] = round(cum_out / cum_model_elapsed, 2)
    state["inv_captured"] = True
    state["last_updated"] = now
    state["inv_active_tools"] = {}
    state["inv_tool_intervals"] = []
    return True


def safe_id(session_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", session_id or "default")
    return cleaned[:128] or "default"


def fmt_tokens(n: int | float) -> str:
    n = int(round(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    if n >= 1_000:
        return f"{n / 1_000:.2f}k"
    return str(n)


def calc_cache_rate(
    uncached_input: int,
    cache_read: int,
    cache_create: int = 0,
) -> float | None:
    denom = uncached_input + cache_read + cache_create
    if denom <= 0:
        return None
    return (cache_read / denom) * 100.0


def parse_iso_local(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone()
    except Exception:
        return None


@contextmanager
def session_lock(session_id: str):
    """Advisory file lock for atomic timing state updates."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE_DIR / f"{safe_id(session_id)}.lock"
    f = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def load_json_safe(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def find_transcript_path(session_id: str, explicit_path: str = "") -> Path | None:
    if explicit_path:
        p = Path(explicit_path)
        if p.name == "transcript_full.jsonl":
            compact = p.with_name("transcript.jsonl")
            if compact.is_file():
                return compact
        if p.is_file():
            return p
    if session_id:
        candidate = BRAIN_DIR / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        if candidate.is_file():
            return candidate
    return None


def read_transcript_incremental(transcript_path: Path, session_id: str) -> dict:
    """Incrementally aggregate USER_INPUT turns and PLANNER_RESPONSE steps."""
    cache_file = CACHE_DIR / f"{safe_id(session_id)}_tcache.json"
    cached = load_json_safe(cache_file) if cache_file.is_file() else {}

    try:
        st = transcript_path.stat()
    except OSError:
        return {
            "user_turns": 0,
            "turns": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "est_cost_usd": 0.0,
            "last_output_tokens": 0,
            "last_ctx_tokens": 0,
            "last_model": "",
            "last_created_at": "",
        }

    same_file = (
        cached.get("version") == TCACHE_VERSION
        and "est_cost_usd" in cached
        and cached.get("path") == str(transcript_path)
        and cached.get("inode") == st.st_ino
        and cached.get("offset", 0) <= st.st_size
    )
    offset = cached.get("offset", 0) if same_file else 0
    user_turns = cached.get("user_turns", 0) if same_file else 0
    turns = cached.get("turns", 0) if same_file else 0
    in_tok = cached.get("input_tokens", 0) if same_file else 0
    cr_tok = cached.get("cache_read_tokens", 0) if same_file else 0
    out_tok = cached.get("output_tokens", 0) if same_file else 0
    est_cost = float(cached.get("est_cost_usd", 0.0)) if same_file else 0.0
    last_out = cached.get("last_output_tokens", 0) if same_file else 0
    last_ctx = cached.get("last_ctx_tokens", 0) if same_file else 0
    last_model = cached.get("last_model", "") if same_file else ""
    last_created_at = cached.get("last_created_at", "") if same_file else ""

    if offset < st.st_size:
        try:
            with transcript_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            last_nl = chunk.rfind(b"\n")
            if last_nl != -1:
                complete = chunk[: last_nl + 1]
                offset += len(complete)
                for raw_line in complete.splitlines():
                    has_plan = b'"PLANNER_RESPONSE"' in raw_line
                    has_user = b'"USER_INPUT"' in raw_line
                    if not (has_plan or has_user):
                        continue
                    try:
                        obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    step_type = obj.get("type")
                    if step_type == "USER_INPUT":
                        user_turns += 1
                    elif step_type == "PLANNER_RESPONSE":
                        turns += 1
                        step_in = int(obj.get("input_tokens") or 0)
                        step_cr = int(obj.get("cache_read_tokens") or 0)
                        step_out = int(obj.get("output_tokens") or 0)
                        in_tok += step_in
                        cr_tok += step_cr
                        out_tok += step_out
                        last_out = step_out
                        last_ctx = step_in + step_cr + step_out
                        step_model = obj.get("model") or ""
                        if step_model:
                            last_model = step_model
                        if obj.get("created_at"):
                            last_created_at = str(obj.get("created_at"))
                        est_cost += calc_step_cost_usd(
                            step_model or last_model,
                            step_in,
                            step_cr,
                            step_out,
                            obj.get("created_at") or "",
                        )
        except OSError:
            pass

        result = {
            "version": TCACHE_VERSION,
            "path": str(transcript_path),
            "inode": st.st_ino,
            "offset": offset,
            "user_turns": user_turns,
            "turns": turns,
            "input_tokens": in_tok,
            "cache_read_tokens": cr_tok,
            "output_tokens": out_tok,
            "est_cost_usd": round(est_cost, 6),
            "last_output_tokens": last_out,
            "last_ctx_tokens": last_ctx,
            "last_model": last_model,
            "last_created_at": last_created_at,
        }
        try:
            atomic_write_json(cache_file, result)
        except OSError:
            pass
        return result

    return {
        "user_turns": user_turns,
        "turns": turns,
        "input_tokens": in_tok,
        "cache_read_tokens": cr_tok,
        "output_tokens": out_tok,
        "est_cost_usd": round(est_cost, 6),
        "last_output_tokens": last_out,
        "last_ctx_tokens": last_ctx,
        "last_model": last_model,
        "last_created_at": last_created_at,
    }


def parse_transcript_full(transcript_path: Path) -> dict:
    """Parse all USER_INPUT turns and PLANNER_RESPONSE steps from transcript.jsonl."""
    steps = []
    user_turns = 0
    user_dates = []
    in_tok = 0
    cr_tok = 0
    out_tok = 0
    est_cost = 0.0
    first_dt = None
    last_dt = None
    by_model: dict[str, dict[str, float]] = {}

    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                has_plan = '"PLANNER_RESPONSE"' in line
                has_user = '"USER_INPUT"' in line
                if not (has_plan or has_user):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                step_type = obj.get("type")
                if step_type == "USER_INPUT":
                    user_turns += 1
                    udt = parse_iso_local(obj.get("created_at") or "")
                    if udt:
                        user_dates.append(udt.date())
                        if first_dt is None or udt < first_dt:
                            first_dt = udt
                        if last_dt is None or udt > last_dt:
                            last_dt = udt
                    continue
                if step_type != "PLANNER_RESPONSE":
                    continue
                inp = int(obj.get("input_tokens") or 0)
                cr = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                model = obj.get("model") or "unknown"
                ts = obj.get("created_at") or ""
                step_cost = calc_step_cost_usd(model, inp, cr, out, ts)
                dt = parse_iso_local(ts)
                if dt:
                    if first_dt is None or dt < first_dt:
                        first_dt = dt
                    if last_dt is None or dt > last_dt:
                        last_dt = dt
                in_tok += inp
                cr_tok += cr
                out_tok += out
                est_cost += step_cost
                m_bucket = by_model.setdefault(
                    model,
                    {"turns": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                )
                m_bucket["turns"] += 1
                m_bucket["input_tokens"] += inp
                m_bucket["cache_read_tokens"] += cr
                m_bucket["output_tokens"] += out
                m_bucket["cost_usd"] += step_cost
                steps.append(
                    {
                        "step_index": obj.get("step_index"),
                        "created_at": ts,
                        "dt": dt,
                        "model": model,
                        "input_tokens": inp,
                        "cache_read_tokens": cr,
                        "output_tokens": out,
                        "cost_usd": step_cost,
                    }
                )
    except OSError:
        pass

    return {
        "user_turns": user_turns,
        "user_dates": user_dates,
        "turns": len(steps),
        "input_tokens": in_tok,
        "cache_read_tokens": cr_tok,
        "output_tokens": out_tok,
        "est_cost_usd": est_cost,
        "first_dt": first_dt,
        "last_dt": last_dt,
        "by_model": by_model,
        "steps": steps,
    }


def _normalize_workspace_uri(uri: str) -> str:
    if uri.startswith("file://"):
        raw_path = unquote(urlparse(uri).path)
        return os.path.realpath(raw_path) if raw_path else ""
    return uri


def load_conversation_metadata() -> dict[str, dict]:
    """Read session titles and workspace paths from conversation_summaries.db."""
    meta: dict[str, dict] = {}
    if not SUMMARIES_DB.is_file():
        return meta
    conn = None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(
            "SELECT conversation_id, title, workspace_uris, parent_conversation_id "
            "FROM conversation_summaries"
        ).fetchall()
        for cid, title, w_json, pid in rows:
            workspaces = []
            if w_json:
                try:
                    raw_uris = json.loads(w_json)
                    for u in raw_uris:
                        norm = _normalize_workspace_uri(str(u))
                        if norm and norm not in workspaces:
                            workspaces.append(norm)
                except Exception:
                    pass
            meta[cid] = {
                "title": title or "",
                "workspaces": workspaces,
                "parent_id": pid or "",
            }
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return meta


def _query_descendant_tree(root_session_id: str) -> tuple[list[str], dict[str, str]]:
    """Return (descendant_ids, agent_names_by_id) from conversation_summaries.db."""
    if not root_session_id or not SUMMARIES_DB.is_file():
        return [], {}

    children_map: dict[str, list[str]] = {}
    agent_names: dict[str, str] = {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT conversation_id, parent_conversation_id, agent_name "
                "FROM conversation_summaries WHERE parent_conversation_id != ''"
            ).fetchall()
            for cid, pid, aname in rows:
                if cid and pid:
                    children_map.setdefault(pid, []).append(cid)
                    if aname:
                        agent_names[cid] = str(aname)
        except sqlite3.Error:
            for cid, pid in conn.execute(
                "SELECT conversation_id, parent_conversation_id "
                "FROM conversation_summaries WHERE parent_conversation_id != ''"
            ):
                if cid and pid:
                    children_map.setdefault(pid, []).append(cid)
        try:
            for cid, pid in conn.execute(
                "SELECT conversation_id, parent_id FROM conversation_parents"
            ):
                if cid and pid:
                    bucket = children_map.setdefault(pid, [])
                    if cid not in bucket:
                        bucket.append(cid)
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return [], {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    descendants: list[str] = []
    seen = {root_session_id}
    queue = list(children_map.get(root_session_id, []))
    while queue:
        cid = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        descendants.append(cid)
        for grandchild in children_map.get(cid, []):
            if grandchild not in seen:
                queue.append(grandchild)
    return descendants, agent_names


def get_descendant_conversation_ids(root_session_id: str) -> list[str]:
    """Recursively find all descendant subagent conversation IDs from conversation_summaries.db."""
    descendants, _ = _query_descendant_tree(root_session_id)
    return descendants


def read_subagent_aggregates(root_session_id: str) -> dict:
    """Incrementally aggregate transcript stats and hook timings across all descendant subagents."""
    agg = {
        "sub_count": 0,
        "turns": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "est_cost_usd": 0.0,
        "roles": {},
        "timed_out": 0,
        "timed_elapsed": 0.0,
        "model_elapsed": 0.0,
        "tool_elapsed": 0.0,
        "avg_tps": None,
        "last_tps": None,
        "last_elapsed": None,
    }
    latest_updated = -1.0
    descendants, agent_names = _query_descendant_tree(root_session_id)
    for sub_id in descendants:
        sub_path = find_transcript_path(sub_id)
        if sub_path is None or not sub_path.is_file():
            continue
        with session_lock(sub_id):
            st = read_transcript_incremental(sub_path, sub_id)
        if st.get("turns", 0) <= 0 and st.get("user_turns", 0) <= 0:
            continue
        agg["sub_count"] += 1
        agg["turns"] += int(st.get("turns") or 0)
        agg["input_tokens"] += int(st.get("input_tokens") or 0)
        agg["cache_read_tokens"] += int(st.get("cache_read_tokens") or 0)
        agg["output_tokens"] += int(st.get("output_tokens") or 0)
        agg["est_cost_usd"] += float(st.get("est_cost_usd") or 0.0)
        role = agent_names.get(sub_id) or ""
        if role:
            agg["roles"][role] = agg["roles"].get(role, 0) + 1

        t_cache = load_json_safe(CACHE_DIR / f"{safe_id(sub_id)}.json")
        c_out = int(t_cache.get("cum_out") or 0)
        c_el = float(t_cache.get("cum_elapsed") or 0.0)
        c_m_el = float(t_cache.get("cum_model_elapsed") or 0.0)
        c_t_el = float(t_cache.get("cum_tool_elapsed") or 0.0)
        if c_out > 0 and c_el > 0:
            agg["timed_out"] += c_out
            agg["timed_elapsed"] += c_m_el if c_m_el > 0 else c_el
        if c_m_el > 0 or c_t_el > 0:
            agg["model_elapsed"] += c_m_el
            agg["tool_elapsed"] += c_t_el
        l_upd = float(t_cache.get("last_updated") or 0.0)
        if t_cache.get("last_tps") is not None and l_upd >= latest_updated:
            latest_updated = l_upd
            agg["last_tps"] = float(t_cache["last_tps"])
            agg["last_elapsed"] = float(t_cache.get("last_elapsed") or 0.0)

    if agg["timed_elapsed"] > 0 and agg["timed_out"] > 0:
        agg["avg_tps"] = round(agg["timed_out"] / agg["timed_elapsed"], 2)
    return agg
