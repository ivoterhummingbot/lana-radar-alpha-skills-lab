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

from radar_alpha_skills_lab.old_radar_replay import run_fixed_old_shadow_replay, write_replay_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay fixed old-radar 24h shadows with Binance USDT-M 15m OHLC")
    parser.add_argument("--out-prefix", default="output/old-radar-fixed-shadow-24h-ohlc-replay")
    args = parser.parse_args()
    result = run_fixed_old_shadow_replay()
    json_path, md_path = write_replay_outputs(result, args.out_prefix)
    print(f"wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
