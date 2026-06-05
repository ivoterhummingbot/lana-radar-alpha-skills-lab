#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from radar_alpha_skills_lab.config import OUTPUT_DIR
from radar_alpha_skills_lab.score_regime import run_score_regime_audit, write_score_regime_outputs


def main() -> int:
    ap = argparse.ArgumentParser(description="Deep-dive score ranking and regime candidates.")
    ap.add_argument("--hours", type=float, default=24.0, help="Lookback hours; <=0 means all available completed path data.")
    ap.add_argument("--sims", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20260601)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--out-prefix", type=Path, default=OUTPUT_DIR / "score-regime-alpha-search")
    args = ap.parse_args()

    result = run_score_regime_audit(
        hours=None if args.hours <= 0 else args.hours,
        sims=args.sims,
        seed=args.seed,
        top_n=args.top_n,
    )
    json_path, md_path = write_score_regime_outputs(result, args.out_prefix)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print({"candidate_count": result["candidate_count"], "alpha_pass_count": result["alpha_pass_count"]})
    if result["top_candidates"]:
        print(result["top_candidates"][0]["name"], result["top_candidates"][0]["pass_flags"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
