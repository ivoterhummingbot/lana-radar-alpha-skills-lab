from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.old_radar_alpha import CandidateRule, candidate_rows, summarize_old_candidate


def row(ts: str, symbol: str, *, status: str, action: str, final: float, market: float, momentum: float, ret: float):
    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    return {
        "ts_dt": dt,
        "ts": dt.isoformat(),
        "symbol": symbol,
        "decision_status": status,
        "recommended_action": action,
        "final_score": final,
        "market_confirmation_score": market,
        "momentum_confirmation_score": momentum,
        "session": "core_night",
        "return_1h": ret,
    }


class OldRadarAlphaTest(unittest.TestCase):
    def test_candidate_rows_filters_gate_and_top_fraction_per_timestamp(self):
        rows = [
            row("2026-06-01T00:00:00", "A", status="watch_hot", action="probe_watch_runner", final=90, market=70, momentum=60, ret=0.02),
            row("2026-06-01T00:00:00", "B", status="watch_hot", action="probe_watch_runner", final=10, market=30, momentum=20, ret=-0.01),
            row("2026-06-01T00:00:00", "C", status="fomo_blowoff_risk", action="avoid_chase", final=99, market=99, momentum=99, ret=0.03),
        ]
        rule = CandidateRule(
            name="watch_hot_top50_final",
            gate_field="decision_status",
            gate_values=("watch_hot",),
            score_field="final_score",
            top_fraction=0.5,
            horizon="1h",
        )

        selected = candidate_rows(rows, rule)

        self.assertEqual([r["symbol"] for r in selected], ["A"])

    def test_summarize_old_candidate_reports_cap_and_random_validation(self):
        rows = [
            row("2026-06-01T00:00:00", "A", status="watch_hot", action="probe_watch_runner", final=90, market=70, momentum=60, ret=0.02),
            row("2026-06-01T00:00:00", "B", status="watch_hot", action="watch", final=10, market=30, momentum=20, ret=-0.01),
            row("2026-06-01T01:00:00", "C", status="watch_hot", action="probe_watch_runner", final=90, market=70, momentum=60, ret=0.01),
            row("2026-06-01T01:00:00", "D", status="watch_hot", action="watch", final=10, market=30, momentum=20, ret=-0.01),
        ]
        rule = CandidateRule(
            name="runner_action",
            gate_field="recommended_action",
            gate_values=("probe_watch_runner",),
            horizon="1h",
        )

        summary = summarize_old_candidate(rows, rule, sims=10, seed=1, min_rows=1)

        self.assertEqual(summary["name"], "runner_action")
        self.assertEqual(summary["rows"], 2)
        self.assertIn("all_taker8", summary)
        self.assertIn("capacity", summary)
        self.assertIn("random_p95", summary)
        self.assertTrue(summary["all_taker8"]["avg"] > 0)


if __name__ == "__main__":
    unittest.main()
