from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.signal_control import (  # noqa: E402
    cap_portfolio,
    cooldown_symbol,
    build_signal_and_controls,
    stat,
)


def row(ts: datetime, symbol: str, pnl: float, stage: str, score: float) -> dict:
    return {
        "ts_dt": ts,
        "ts": ts.isoformat(),
        "symbol": symbol,
        "managed_1h": pnl,
        "prev5m_stage": stage,
        "unified_discovery_score": score,
    }


class SignalControlTest(unittest.TestCase):
    def test_stat_reports_avg_win_comp_and_mdd(self) -> None:
        result = stat([0.01, -0.02, 0.03])
        self.assertEqual(result["n"], 3)
        self.assertAlmostEqual(result["sum"], 0.02)
        self.assertAlmostEqual(result["avg"], 0.02 / 3)
        self.assertAlmostEqual(result["win"], 2 / 3)
        self.assertLess(result["mdd"], 0)
        self.assertAlmostEqual(result["comp"], (1.01 * 0.98 * 1.03) - 1)

    def test_cooldown_symbol_keeps_first_symbol_row_inside_window(self) -> None:
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = [
            row(base, "AAA", 0.01, "cold", 90),
            row(base + timedelta(minutes=30), "AAA", 0.02, "cold", 95),
            row(base + timedelta(minutes=61), "AAA", -0.01, "cold", 80),
            row(base + timedelta(minutes=10), "BBB", 0.03, "cold", 70),
        ]
        kept = cooldown_symbol(rows, minutes=60)
        self.assertEqual([(r["symbol"], r["ts_dt"].minute) for r in kept], [("AAA", 0), ("BBB", 10), ("AAA", 1)])

    def test_build_signal_and_controls_matches_timestamp_counts(self) -> None:
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = [
            row(base, "A", 0.01, "cold", 10),
            row(base, "B", 0.02, "preconfirmed_5m", 99),
            row(base, "C", 0.03, "cold", 90),
            row(base + timedelta(minutes=5), "D", -0.01, "cold", 5),
            row(base + timedelta(minutes=5), "E", 0.04, "preconfirmed_5m", 100),
        ]
        sets = build_signal_and_controls(rows, seed=7)
        self.assertEqual(len(sets["signal_not_momentum"]), 3)
        self.assertEqual(len(sets["control_score_top_matched"]), 3)
        self.assertEqual(len(sets["control_random_matched"]), 3)
        self.assertEqual([r["symbol"] for r in sets["control_score_top_matched"]], ["B", "C", "E"])
        self.assertEqual(len(sets["control_all_snapshots"]), 5)
        self.assertEqual(len(sets["ablation_prev5m_preconfirmed"]), 2)

    def test_cap_portfolio_uses_score_rank_and_skips_when_capacity_full(self) -> None:
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = [
            row(base, "LOW", 0.50, "cold", 1),
            row(base, "HIGH", 0.10, "cold", 100),
            row(base + timedelta(minutes=30), "MID", 0.20, "cold", 50),
            row(base + timedelta(minutes=61), "NEXT", 0.30, "cold", 20),
        ]
        result = cap_portfolio(rows, pnl_key="managed_1h", cost=0.0, cap=1)
        self.assertEqual(result["taken"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["taken_symbols"], ["HIGH", "NEXT"])
        self.assertAlmostEqual(result["slot_stat"]["sum"], 0.40)


if __name__ == "__main__":
    unittest.main()
