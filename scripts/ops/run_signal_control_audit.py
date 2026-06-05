#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
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
from radar_alpha_skills_lab.signal_control import parse_ts, run_signal_control_audit, write_outputs


def main() -> int:
    ap = argparse.ArgumentParser(description="Run AlphaGBM-style signal/control audit on new-radar snapshots.")
    ap.add_argument("--hours", type=float, default=24.0, help="Lookback hours ending at latest completed snapshot. Use <=0 for all history.")
    ap.add_argument("--sims", type=int, default=1000, help="Random same-timestamp baseline simulations.")
    ap.add_argument("--seed", type=int, default=20260601)
    ap.add_argument("--now-utc", default=None)
    ap.add_argument("--out-prefix", type=Path, default=OUTPUT_DIR / "new-radar-signal-control-audit")
    args = ap.parse_args()

    now = parse_ts(args.now_utc) if args.now_utc else datetime.now(timezone.utc)
    hours = None if args.hours <= 0 else args.hours
    result = run_signal_control_audit(hours=hours, sims=args.sims, seed=args.seed, now_utc=now)
    json_path, md_path = write_outputs(result, args.out_prefix)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(result["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
