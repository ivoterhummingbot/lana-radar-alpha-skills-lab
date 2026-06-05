from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.old_radar_replay import ceil_next_interval, simulate_24h_exit


def bar(ts: str, open_: float, high: float, low: float, close: float):
    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    open_ms = int(dt.timestamp() * 1000)
    close_ms = open_ms + 15 * 60 * 1000 - 1
    return [open_ms, str(open_), str(high), str(low), str(close), "0", close_ms]


class OldRadarReplayTest(unittest.TestCase):
    def test_ceil_next_interval_moves_to_next_15m_open(self):
        dt = datetime(2026, 6, 1, 0, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(ceil_next_interval(dt, minutes=15).isoformat(), "2026-06-01T00:15:00+00:00")
        exact = datetime(2026, 6, 1, 0, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(ceil_next_interval(exact, minutes=15), exact)

    def test_hold24h_uses_next_15m_entry_and_time_exit(self):
        bars = [
            bar("2026-06-01T00:00:00", 100, 100, 100, 100),
            bar("2026-06-01T00:15:00", 100, 105, 99, 102),
            bar("2026-06-02T00:15:00", 110, 112, 108, 110),
        ]
        result = simulate_24h_exit(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), strategy="hold24h")
        self.assertAlmostEqual(result["pnl"], 0.10)
        self.assertEqual(result["reason"], "time_exit")

    def test_tp_half_protect_is_stop_first_before_tp(self):
        bars = [
            bar("2026-06-01T00:15:00", 100, 106, 93, 104),
            bar("2026-06-02T00:15:00", 100, 100, 100, 100),
        ]
        result = simulate_24h_exit(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), strategy="tp5_half_protect")
        self.assertAlmostEqual(result["pnl"], -0.06)
        self.assertEqual(result["reason"], "hard_sl")

    def test_tp_half_protect_realizes_half_and_protects_tail(self):
        bars = [
            bar("2026-06-01T00:15:00", 100, 106, 100, 105),
            bar("2026-06-01T00:30:00", 105, 106, 99, 100),
            bar("2026-06-02T00:15:00", 100, 100, 100, 100),
        ]
        result = simulate_24h_exit(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc), strategy="tp5_half_protect")
        self.assertAlmostEqual(result["pnl"], 0.025)
        self.assertEqual(result["reason"], "protect_after_tp")


if __name__ == "__main__":
    unittest.main()
