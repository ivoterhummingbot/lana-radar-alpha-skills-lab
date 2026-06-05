from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.radar_execution_search import ExecutionRule, cap_portfolio_by_entry, simulate_execution_rule


def bar(ts: str, open_: float, high: float, low: float, close: float):
    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    return [int(dt.timestamp() * 1000), str(open_), str(high), str(low), str(close), "0", int(dt.timestamp() * 1000) + 899999]


class RadarExecutionSearchTest(unittest.TestCase):
    def test_tp_is_stop_first_inside_same_15m_bar(self):
        bars = [
            bar("2026-06-01T00:15:00", 100, 103, 97, 102),
            bar("2026-06-01T00:30:00", 102, 103, 101, 102),
        ]
        rule = ExecutionRule(name="tp2_sl2_1h", max_minutes=60, tp=0.02, sl=-0.02)
        out = simulate_execution_rule(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), rule)
        self.assertEqual(out["reason"], "sl")
        self.assertAlmostEqual(out["pnl"], -0.02)

    def test_tp_then_protect_exits_at_protect_when_later_bar_reverses(self):
        bars = [
            bar("2026-06-01T00:15:00", 100, 103, 100, 102),
            bar("2026-06-01T00:30:00", 102, 103, 100.4, 101),
        ]
        rule = ExecutionRule(name="tp2_protect05_1h", max_minutes=60, tp=0.02, sl=-0.03, protect_after_tp=0.005)
        out = simulate_execution_rule(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), rule)
        self.assertEqual(out["reason"], "protect_after_tp")
        self.assertAlmostEqual(out["pnl"], 0.005)

    def test_time_exit_uses_last_close_within_max_minutes(self):
        bars = [
            bar("2026-06-01T00:15:00", 100, 101, 99, 100.5),
            bar("2026-06-01T00:30:00", 100.5, 101, 99, 101.5),
            bar("2026-06-01T00:45:00", 101.5, 102, 101, 102),
        ]
        rule = ExecutionRule(name="time30", max_minutes=30)
        out = simulate_execution_rule(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), rule)
        self.assertEqual(out["reason"], "time_exit")
        self.assertAlmostEqual(out["pnl"], 0.015)

    def test_cap_portfolio_scales_each_position_by_capacity(self):
        rows = [
            {"ts_dt": datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc), "symbol": "A", "pnl": 0.10, "score": 2},
            {"ts_dt": datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc), "symbol": "B", "pnl": 0.20, "score": 1},
            {"ts_dt": datetime(2026, 6, 1, 0, 15, tzinfo=timezone.utc), "symbol": "C", "pnl": 0.30, "score": 3},
        ]
        out = cap_portfolio_by_entry(rows, "pnl", cap=2, hold_minutes=60, cost=0.0, rank_key="score")
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped"], 1)
        self.assertAlmostEqual(out["slot_stat"]["sum"], 0.15)  # A 0.10/2 + B 0.20/2


if __name__ == "__main__":
    unittest.main()
