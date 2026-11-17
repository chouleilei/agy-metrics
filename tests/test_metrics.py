"""Unit and regression tests for agy-metrics."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agy_metrics_lib as lib  # noqa: E402


class TestMetricsLib(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.orig_cache = lib.CACHE_DIR
        self.orig_brain = lib.BRAIN_DIR
        lib.CACHE_DIR = self.base / "cache"
        lib.BRAIN_DIR = self.base / "brain"
        lib.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lib.BRAIN_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        lib.CACHE_DIR = self.orig_cache
        lib.BRAIN_DIR = self.orig_brain
        self.tmp.cleanup()

    def test_cache_hit_rate(self):
        self.assertIsNone(lib.calc_cache_rate(0, 0, 0))
        self.assertAlmostEqual(lib.calc_cache_rate(1000, 9000), 90.0)
        self.assertAlmostEqual(lib.calc_cache_rate(1000, 8000, 1000), 80.0)

    def test_incremental_matches_full_cost_and_turns(self):
        tp = self.base / "transcript.jsonl"
        lines = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-17T08:00:00Z"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-17T08:00:05Z",
                "model": "gemini-4-hydrogen-xhigh",
                "input_tokens": 100000,
                "cache_read_tokens": 900000,
                "output_tokens": 10000,
            },
            {"step_index": 2, "type": "USER_INPUT", "created_at": "2026-11-17T08:01:00Z"},
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-17T08:01:04Z",
                "model": "claude-sonnet-5-5-high",
                "input_tokens": 50000,
                "cache_read_tokens": 150000,
                "output_tokens": 5000,
            },
        ]
        tp.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

        inc = lib.read_transcript_incremental(tp, "sess-1")
        full = lib.parse_transcript_full(tp)
        self.assertEqual(inc["user_turns"], 2)
        self.assertEqual(inc["turns"], 2)
        self.assertEqual(inc["input_tokens"], full["input_tokens"])
        self.assertEqual(inc["cache_read_tokens"], full["cache_read_tokens"])
        self.assertEqual(inc["output_tokens"], full["output_tokens"])
        self.assertAlmostEqual(inc["est_cost_usd"], full["est_cost_usd"], places=5)
        # Step 1: 0.1*1.5 + 0.9*0.075 + 0.01*7.5 = 0.2925
        # Step 2: 0.05*2.0 + 0.15*0.10 + 0.005*10.0 = 0.165
        self.assertAlmostEqual(full["est_cost_usd"], 0.4575, places=5)

    def test_merge_intervals_and_duration_format(self):
        self.assertAlmostEqual(lib.merge_intervals_duration([[0.0, 5.0], [2.0, 8.0], [10.0, 12.0]]), 10.0)
        self.assertAlmostEqual(lib.merge_intervals_duration([]), 0.0)
        self.assertEqual(lib.fmt_duration(4.26), "4.3s")
        self.assertEqual(lib.fmt_duration(75.0), "1m15s")
        self.assertEqual(lib.fmt_duration(3665.0), "1h01m")


class TestEndToEndScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agy_dir = self.base / "antigravity-cli"
        self.brain = self.agy_dir / "brain"
        self.cache = self.agy_dir / "cache" / "agy_metrics"
        self.brain.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ, AGY_DATA_DIR=str(self.agy_dir), AGY_NO_QUOTA_REFRESH="1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_statusline_and_hooks_regression(self):
        sid = "conv-e2e-1"
        logs_dir = self.brain / sid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        tp = logs_dir / "transcript.jsonl"
        tp_full = logs_dir / "transcript_full.jsonl"

        step0 = json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-17T08:00:00Z"}) + "\n"
        step1 = json.dumps({
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-17T08:00:01Z",
            "model": "gemini-4-hydrogen-xhigh",
            "input_tokens": 10000,
            "cache_read_tokens": 90000,
            "output_tokens": 500,
        }) + "\n"
        tp.write_text(step0 + step1, encoding="utf-8")
        tp_full.write_text(step0 + step1, encoding="utf-8")

        # Pre-seed quota cache
        (self.cache / "quota_cache.json").write_text(
            json.dumps({"gemini_weekly_pct": 72.5, "fetched_at": time.time()}),
            encoding="utf-8",
        )

        # PreInvocation receives transcript_full.jsonl
        r1 = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
            cwd=str(REPO_ROOT),
            input=json.dumps({
                "conversationId": sid,
                "hookEventName": "PreInvocation",
                "transcriptPath": str(tp_full),
                "invocationNum": 2,
            }),
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual(r1.stdout.strip(), "{}")

        time.sleep(0.15)
        # Inflated stdin.total_output_tokens before transcript updates must NOT corrupt speed
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "statusline.py")],
            input=json.dumps({
                "session_id": sid,
                "transcript_path": str(tp),
                "context_window": {
                    "context_window_size": 1048576,
                    "used_percentage": 12.1,
                    "total_output_tokens": 999999,
                },
            }),
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        state_mid = json.loads((self.cache / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertFalse(state_mid.get("inv_captured"))

        # Step 2 lands in transcript
        step2 = json.dumps({
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-17T08:00:02Z",
            "model": "gemini-4-hydrogen-xhigh",
            "input_tokens": 2000,
            "cache_read_tokens": 98000,
            "output_tokens": 100,
        }) + "\n"
        with tp.open("a", encoding="utf-8") as f:
            f.write(step2)
        with tp_full.open("a", encoding="utf-8") as f:
            f.write(step2)

        r_stop = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
            cwd=str(REPO_ROOT),
            input=json.dumps({
                "conversationId": sid,
                "hookEventName": "Stop",
                "transcriptPath": str(tp_full),
            }),
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual(r_stop.stdout.strip(), '{"decision": "allow"}')

        r_sl = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "statusline.py")],
            input=json.dumps({
                "session_id": sid,
                "transcript_path": str(tp),
                "context_window": {"context_window_size": 1048576, "used_percentage": 12.1},
            }),
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        out = r_sl.stdout
        self.assertIn("24.2%", out)       # 12.1% on 1M -> 24.2% on 512K
        self.assertIn("1轮2步", out)
        self.assertIn("平均", out)
        self.assertIn("周余", out)
        self.assertIn("72.5%", out)
        self.assertNotIn("Hydrogen", out)  # model name omitted from statusLine

        state_done = json.loads((self.cache / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertEqual(state_done["last_out_tokens"], 100)
        self.assertIn("gemini-4-hydrogen-xhigh", state_done["by_model"])

        # Verify agy-stats CLI & JSON output
        r_stats = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "agy-stats"), "--json"],
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        stats_json = json.loads(r_stats.stdout)
        self.assertEqual(stats_json["sessions_count"], 1)
        self.assertIsNotNone(stats_json["by_model"]["gemini-4-hydrogen-xhigh"]["avg_tps"])

    def test_subagent_recursive_aggregation(self):
        root_id = "conv-root-1"
        child_id = "conv-child-1"
        grandchild_id = "conv-grandchild-1"

        def write_tx(cid: str, lines: list[dict]) -> Path:
            d = self.brain / cid / ".system_generated" / "logs"
            d.mkdir(parents=True, exist_ok=True)
            p = d / "transcript.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            return p

        root_tp = write_tx(root_id, [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-17T08:00:00Z"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-17T08:00:02Z",
                "model": "gemini-4-hydrogen-xhigh",
                "input_tokens": 10000,
                "cache_read_tokens": 90000,
                "output_tokens": 500,
            },
        ])
        write_tx(child_id, [
            {
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-17T08:00:05Z",
                "model": "gemini-4-hydrogen-xhigh",
                "input_tokens": 20000,
                "cache_read_tokens": 80000,
                "output_tokens": 1000,
            },
        ])
        write_tx(grandchild_id, [
            {
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-17T08:00:08Z",
                "model": "gemini-3.8-flash",
                "input_tokens": 5000,
                "cache_read_tokens": 15000,
                "output_tokens": 300,
            },
        ])

        db_path = self.agy_dir / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT, workspace_uris TEXT, "
            "parent_conversation_id TEXT, agent_name TEXT)"
        )
        ws_json = json.dumps(["file:///root/agy-metrics"])
        conn.executemany(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
            [
                (root_id, "主编排任务", ws_json, "", ""),
                (child_id, "", ws_json, root_id, "research"),
                (grandchild_id, "", ws_json, child_id, "self"),
            ],
        )
        conn.commit()
        conn.close()

        (self.cache / f"{child_id}.json").write_text(
            json.dumps({
                "cum_out": 1000,
                "cum_elapsed": 20.0,
                "cum_tps": 50.0,
                "last_tps": 25.0,
                "last_elapsed": 4.0,
                "last_updated": 100.0,
            }),
            encoding="utf-8",
        )
        (self.cache / f"{grandchild_id}.json").write_text(
            json.dumps({
                "cum_out": 300,
                "cum_elapsed": 6.0,
                "cum_tps": 50.0,
                "last_tps": 30.0,
                "last_elapsed": 2.0,
                "last_updated": 200.0,
            }),
            encoding="utf-8",
        )

        r_sl = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "statusline.py")],
            input=json.dumps({
                "session_id": root_id,
                "transcript_path": str(root_tp),
                "context_window": {"context_window_size": 1048576, "used_percentage": 10.0},
            }),
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        lines = r_sl.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        # Line 1: main session only
        self.assertIn("1轮1步", lines[0])
        self.assertIn("In:100.0k", lines[0])
        self.assertIn("Out:500", lines[0])
        # Line 2: subagent summary + combined total
        self.assertIn("↳ 子代理(2个·research×1,self×1/2步)", lines[1])
        plain_line2 = re.sub(r"\x1b\[[0-9;]*m", "", lines[1])
        self.assertIn("In:120.0k", plain_line2)
        self.assertIn("Out:1.30k", plain_line2)
        self.assertIn("速度 30.0(2.0s)/平均50.0 tok/s", plain_line2)
        self.assertIn("总计", plain_line2)
        self.assertIn("In:220.0k", lines[1])
        self.assertIn("Out:1.80k", lines[1])

        r_stats = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "agy-stats"), "--json"],
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )
        stats_json = json.loads(r_stats.stdout)
        self.assertEqual(stats_json["sessions_count"], 1)
        s0 = stats_json["sessions"][0]
        self.assertEqual(s0["session_id"], root_id)
        self.assertEqual(s0["subagent_count"], 2)
        self.assertEqual(s0["turns"], 3)
        self.assertEqual(s0["input_tokens"], 35000)
        self.assertEqual(s0["cache_read_tokens"], 185000)
        self.assertEqual(s0["output_tokens"], 1800)

    def test_pretool_posttool_timing_split(self):
        sid = "conv-tool-split"
        logs_dir = self.brain / sid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        tp = logs_dir / "transcript.jsonl"
        tp.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T00:00:00Z"}) + "\n",
            encoding="utf-8",
        )

        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
            input=json.dumps({"conversationId": sid, "hookEventName": "PreInvocation", "transcriptPath": str(tp)}),
            text=True, capture_output=True, env=self.env, check=True,
        )
        time.sleep(0.08)

        # Two overlapping parallel tools
        for tid in ("t1", "t2"):
            r_pre = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
                input=json.dumps({
                    "conversationId": sid,
                    "hookEventName": "PreToolUse",
                    "transcriptPath": str(tp),
                    "toolCall": {"id": tid, "name": "run_command"},
                }),
                text=True, capture_output=True, env=self.env, check=True,
            )
            self.assertEqual(json.loads(r_pre.stdout), {"decision": "allow"})

        time.sleep(0.12)
        for tid in ("t1", "t2"):
            r_post = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
                input=json.dumps({
                    "conversationId": sid,
                    "hookEventName": "PostToolUse",
                    "transcriptPath": str(tp),
                    "toolCall": {"id": tid, "name": "run_command"},
                }),
                text=True, capture_output=True, env=self.env, check=True,
            )
            self.assertEqual(json.loads(r_post.stdout), {})

        with tp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T00:00:01Z",
                "model": "gemini-4-hydrogen-xhigh",
                "input_tokens": 1000,
                "cache_read_tokens": 9000,
                "output_tokens": 200,
            }) + "\n")

        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "record_timing.py")],
            input=json.dumps({"conversationId": sid, "hookEventName": "Stop", "transcriptPath": str(tp)}),
            text=True, capture_output=True, env=self.env, check=True,
        )

        st = json.loads((self.cache / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertGreater(st["cum_tool_elapsed"], 0.10)
        self.assertGreater(st["cum_model_elapsed"], 0.04)
        self.assertLess(st["cum_tool_elapsed"], st["cum_elapsed"])

        r_sl = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "statusline.py")],
            input=json.dumps({"session_id": sid, "transcript_path": str(tp)}),
            text=True, capture_output=True, env=self.env, check=True,
        )
        plain = re.sub(r"\x1b\[[0-9;]*m", "", r_sl.stdout)
        self.assertIn("耗时 模型", plain)
        self.assertIn("/工具", plain)


if __name__ == "__main__":
    unittest.main()
