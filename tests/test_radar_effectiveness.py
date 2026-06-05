from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.radar_effectiveness import candidate_top_fraction_by_ts, simulate_short_horizons


def bar(ts: str, open_: float, high: float, low: float, close: float):
    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    open_ms = int(dt.timestamp() * 1000)
    close_ms = open_ms + 15 * 60 * 1000 - 1
    return [open_ms, str(open_), str(high), str(low), str(close), "0", close_ms]


class RadarEffectivenessTest(unittest.TestCase):
    def test_short_horizon_replay_uses_next_15m_open_and_reports_mfe(self):
        bars = [
            bar("2026-06-01T00:00:00", 90, 91, 89, 90),
            bar("2026-06-01T00:15:00", 100, 104, 99, 102),
            bar("2026-06-01T00:30:00", 102, 108, 101, 106),
            bar("2026-06-01T00:45:00", 106, 107, 104, 105),
            bar("2026-06-01T01:00:00", 105, 110, 104, 109),
        ]
        out = simulate_short_horizons(bars, datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc))
        self.assertAlmostEqual(out["ret_15m"], 0.02)
        self.assertAlmostEqual(out["mfe_15m"], 0.04)
        self.assertAlmostEqual(out["ret_30m"], 0.06)
        self.assertAlmostEqual(out["mfe_30m"], 0.08)
        self.assertAlmostEqual(out["ret_1h"], 0.09)
        self.assertAlmostEqual(out["mfe_1h"], 0.10)

    def test_top_fraction_selects_within_each_timestamp(self):
        ts1 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
        rows = [
            {"ts_dt": ts1, "symbol": "A", "score": 1},
            {"ts_dt": ts1, "symbol": "B", "score": 3},
            {"ts_dt": ts1, "symbol": "C", "score": 2},
            {"ts_dt": ts2, "symbol": "D", "score": 4},
            {"ts_dt": ts2, "symbol": "E", "score": 5},
        ]
        selected = candidate_top_fraction_by_ts(rows, "score", 0.20)
        self.assertEqual([r["symbol"] for r in selected], ["B", "E"])


if __name__ == "__main__":
    unittest.main()
