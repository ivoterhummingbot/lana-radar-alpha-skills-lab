#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.radar_effectiveness import run_radar_effectiveness, write_radar_effectiveness_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate new/old hotcoin radar short-horizon effectiveness")
    parser.add_argument("--sims", type=int, default=200)
    parser.add_argument("--out-prefix", default="output/new-old-radar-short-horizon-effectiveness")
    args = parser.parse_args()
    result = run_radar_effectiveness(sims=args.sims)
    json_path, md_path = write_radar_effectiveness_outputs(result, args.out_prefix)
    print(f"wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
