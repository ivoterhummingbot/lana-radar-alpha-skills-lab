from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.score_regime import (
    CandidateSpec,
    bjt_session,
    candidate_mask,
    describe_candidate,
    top_fraction_per_timestamp,
)


def row(ts: str, symbol: str, score: float, **extra):
    data = {
        "ts_dt": datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        "symbol": symbol,
        "unified_discovery_score": score,
        "btc_gate_permission": "allow",
        "btc_state": "uptrend",
        "hour_bjt": datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone(timezone.utc).hour,
    }
    data.update(extra)
    return data


class ScoreRegimeTest(unittest.TestCase):
    def test_top_fraction_is_selected_inside_each_timestamp(self):
        rows = [
            row("2026-06-01T00:00:00", "A", 0.9),
            row("2026-06-01T00:00:00", "B", 0.8),
            row("2026-06-01T00:00:00", "C", 0.1),
            row("2026-06-01T01:00:00", "D", 0.7),
            row("2026-06-01T01:00:00", "E", 0.2),
        ]

        picked = top_fraction_per_timestamp(rows, "unified_discovery_score", 0.4)

        self.assertEqual([r["symbol"] for r in picked], ["A", "B", "D"])

    def test_candidate_mask_combines_score_fraction_and_regime(self):
        rows = [
            row("2026-06-01T00:00:00", "A", 0.9, btc_gate_permission="allow"),
            row("2026-06-01T00:00:00", "B", 0.8, btc_gate_permission="deny"),
            row("2026-06-01T00:00:00", "C", 0.7, btc_gate_permission="allow"),
            row("2026-06-01T01:00:00", "D", 0.6, btc_gate_permission="allow"),
            row("2026-06-01T01:00:00", "E", 0.5, btc_gate_permission="deny"),
        ]
        spec = CandidateSpec(
            name="score_top40_allow",
            score_field="unified_discovery_score",
            top_fraction=0.4,
            gate_field="btc_gate_permission",
            gate_values=("allow",),
        )

        picked = candidate_mask(rows, spec)

        self.assertEqual([r["symbol"] for r in picked], ["A", "D"])

    def test_bjt_session_classifies_core_and_garbage_windows(self):
        self.assertEqual(bjt_session(21), "core_night")
        self.assertEqual(bjt_session(3), "core_night")
        self.assertEqual(bjt_session(5), "garbage_window")
        self.assertEqual(bjt_session(10), "day_high_threshold")
        self.assertEqual(bjt_session(17), "prewarm")

    def test_describe_candidate_mentions_boundaries(self):
        spec = CandidateSpec(
            name="x",
            score_field="maker_attention_score",
            top_fraction=0.2,
            gate_field="session",
            gate_values=("core_night",),
        )

        text = describe_candidate(spec)

        self.assertIn("maker_attention_score", text)
        self.assertIn("top20", text)
        self.assertIn("session", text)
        self.assertIn("core_night", text)


if __name__ == "__main__":
    unittest.main()
