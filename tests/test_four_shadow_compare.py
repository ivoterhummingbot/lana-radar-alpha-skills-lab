from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.four_shadow_compare import build_four_shadow_sets, summarize_shadow


def row(ts: str, symbol: str, *, hour_bjt: int, score: float, market: float, mom: float, prev5m: float, outcome: float):
    return {
        "ts": datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        "symbol": symbol,
        "hour_bjt": hour_bjt,
        "unified_discovery_score": score,
        "market_confirmation_score": market,
        "momentum_confirmation_score": mom,
        "prev5m_ret": prev5m,
        "managed_1h": outcome,
    }


class FourShadowCompareTest(unittest.TestCase):
    def test_builds_exact_four_named_shadows(self):
        rows = [
            row("2026-06-01T00:00:00", "A", hour_bjt=21, score=10, market=9, mom=8, prev5m=-0.01, outcome=0.01),
            row("2026-06-01T00:00:00", "B", hour_bjt=21, score=9, market=8, mom=9, prev5m=0.01, outcome=0.02),
            row("2026-06-01T00:00:00", "C", hour_bjt=21, score=1, market=1, mom=1, prev5m=-0.01, outcome=-0.01),
            row("2026-06-01T08:00:00", "D", hour_bjt=16, score=10, market=10, mom=10, prev5m=-0.01, outcome=0.03),
        ]

        shadows = build_four_shadow_sets(rows)

        self.assertEqual(
            list(shadows),
            [
                "old_shadow_1_not_momentum_cap5",
                "old_shadow_2_not_momentum_cap20",
                "new_shadow_A_core_top20_momentum_cap5",
                "new_shadow_B_core_top10_market_cap5",
            ],
        )
        self.assertEqual(shadows["old_shadow_1_not_momentum_cap5"].cap, 5)
        self.assertEqual(shadows["old_shadow_2_not_momentum_cap20"].cap, 20)
        self.assertEqual(shadows["new_shadow_A_core_top20_momentum_cap5"].cap, 5)
        self.assertEqual(shadows["new_shadow_B_core_top10_market_cap5"].cap, 5)

    def test_summary_contains_random_baseline_and_capacity(self):
        rows = [
            row("2026-06-01T00:00:00", "A", hour_bjt=21, score=10, market=10, mom=10, prev5m=-0.01, outcome=0.02),
            row("2026-06-01T00:00:00", "B", hour_bjt=21, score=9, market=8, mom=9, prev5m=-0.01, outcome=0.01),
            row("2026-06-01T00:00:00", "C", hour_bjt=21, score=1, market=1, mom=1, prev5m=0.02, outcome=-0.01),
        ]
        shadow = build_four_shadow_sets(rows)["new_shadow_A_core_top20_momentum_cap5"]

        summary = summarize_shadow(shadow, rows, sims=10, seed=1)

        self.assertEqual(summary["name"], "new_shadow_A_core_top20_momentum_cap5")
        self.assertIn("gross", summary)
        self.assertIn("all_taker8", summary)
        self.assertIn("random_p95", summary)
        self.assertIn("capacity", summary)
        self.assertIn("concentration", summary)


if __name__ == "__main__":
    unittest.main()
