#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.blueprint import write_blueprint


def main() -> int:
    path = write_blueprint()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
