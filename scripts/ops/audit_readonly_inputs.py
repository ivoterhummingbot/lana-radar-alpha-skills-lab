#!/usr/bin/env python3
from __future__ import annotations

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

from radar_alpha_skills_lab.data_contract import run_input_audit, write_audit_outputs


def main() -> int:
    audit = run_input_audit()
    json_path, md_path = write_audit_outputs(audit)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"status={'OK' if audit.ok else 'FAIL'}")
    return 0 if audit.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
