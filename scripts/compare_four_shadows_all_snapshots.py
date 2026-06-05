#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.four_shadow_compare import compare_four_shadows_all_snapshots, write_four_shadow_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare four radar shadow lanes over all new-radar snapshot data")
    parser.add_argument("--sims", type=int, default=300)
    parser.add_argument("--out-prefix", default="output/four-shadow-comparison-all-snapshots")
    args = parser.parse_args()
    result = compare_four_shadows_all_snapshots(sims=args.sims)
    json_path, md_path = write_four_shadow_outputs(result, args.out_prefix)
    print(f"wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
