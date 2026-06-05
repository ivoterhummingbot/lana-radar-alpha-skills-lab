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

from radar_alpha_skills_lab.old_radar_alpha import run_old_radar_alpha_search, write_old_radar_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Search old Lana delayed-radar alpha candidates")
    parser.add_argument("--sims", type=int, default=200)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--out-prefix", default="output/old-radar-delayed-alpha-search")
    args = parser.parse_args()
    result = run_old_radar_alpha_search(sims=args.sims, top_n=args.top_n)
    json_path, md_path = write_old_radar_outputs(result, args.out_prefix)
    print(f"wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
