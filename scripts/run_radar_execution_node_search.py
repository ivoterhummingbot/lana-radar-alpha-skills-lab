#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.radar_execution_search import run_execution_node_search, write_execution_node_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Search entry/exit execution nodes for validated hotcoin radar sets")
    parser.add_argument("--sims", type=int, default=300)
    parser.add_argument("--out-prefix", default="output/radar-execution-node-search")
    args = parser.parse_args()
    result = run_execution_node_search(sims=args.sims)
    json_path, md_path = write_execution_node_outputs(result, args.out_prefix)
    print(f"wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
