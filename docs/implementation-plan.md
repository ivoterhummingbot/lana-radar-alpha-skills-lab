# Radar Alpha Skills Lab Implementation Plan

> **For Hermes:** Use this as a clean-room implementation plan. Do not modify `/Users/leon/Documents/Quant/lana-community-hotcoin-analyzer`.

**Goal:** Build a separate read-only research lab that adapts AlphaGBM/skills methods to Lana radar alpha discovery without affecting existing shadows.

**Architecture:** Source project remains the data owner. This project opens source SQLite databases in read-only mode, produces independent JSON/Markdown reports, and stores all outputs locally under this project.

**Tech Stack:** Python 3.11 stdlib first; optional pandas only for later analytics; SQLite URI `mode=ro`; unittest for smoke tests.

---

## Task 1: Create clean project shell

**Objective:** Establish a separate project boundary and documentation.

**Files:**

- Create: `README.md`
- Create: `docs/alphagbm-skills-adaptation.md`
- Create: `docs/implementation-plan.md`

**Verification:**

```bash
python3 - <<'PY'
from pathlib import Path
root=Path('/Users/leon/Documents/Quant/lana-radar-alpha-skills-lab')
assert root.exists()
assert (root/'README.md').exists()
assert (root/'docs/alphagbm-skills-adaptation.md').exists()
print('project shell ok')
PY
```

## Task 2: Add read-only source configuration

**Objective:** Define source paths and required data contracts without importing current project code.

**Files:**

- Create: `src/radar_alpha_skills_lab/config.py`
- Create: `src/radar_alpha_skills_lab/data_contract.py`
- Create: `src/radar_alpha_skills_lab/__init__.py`

**Verification:**

```bash
PYTHONPATH=src python3 - <<'PY'
from radar_alpha_skills_lab.config import DEFAULT_SOURCE
print(DEFAULT_SOURCE.source_root)
PY
```

## Task 3: Add read-only input audit script

**Objective:** Prove the new project can inspect source DBs without write access.

**Files:**

- Create: `scripts/audit_readonly_inputs.py`

**Verification:**

```bash
python3 scripts/audit_readonly_inputs.py
```

Expected:

```text
wrote output/readonly-input-audit.json
wrote output/readonly-input-audit.md
```

## Task 4: Add alpha discovery blueprint generator

**Objective:** Generate a concrete research blueprint from the AlphaGBM method mapping.

**Files:**

- Create: `src/radar_alpha_skills_lab/blueprint.py`
- Create: `scripts/generate_alpha_discovery_blueprint.py`

**Verification:**

```bash
python3 scripts/generate_alpha_discovery_blueprint.py
```

Expected:

```text
wrote output/alpha-discovery-blueprint.md
```

## Task 5: Add smoke tests

**Objective:** Lock the project boundary: source is read-only; reports are local.

**Files:**

- Create: `tests/test_contracts.py`

**Verification:**

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

## Future tasks

### Task 6: Implement BPS-style signal/control analyzer

Create:

- `src/radar_alpha_skills_lab/signal_control.py`
- `scripts/run_signal_control_audit.py`

Inputs:

- `maker_attn_symbol_scores`
- `maker_attn_market_snapshots`

Outputs:

- `output/signal-control-audit-YYYYMMDD.md/json`

### Task 7: Implement regime gate matrix

Create:

- `src/radar_alpha_skills_lab/regime_gate.py`
- `scripts/run_regime_gate_matrix.py`

### Task 8: Implement exit lab adapter

Create:

- `src/radar_alpha_skills_lab/exit_lab.py`
- `scripts/run_exit_lab.py`

### Task 9: Implement fixed composite search

Create:

- `src/radar_alpha_skills_lab/composite_search.py`
- `scripts/run_composite_search.py`

### Task 10: Implement shadow registry

Create:

- `docs/shadow-registry.md`
- `src/radar_alpha_skills_lab/shadow_registry.py`

Registry fields:

```text
name
layer: discovery/execution
rule
exit
venue
fee_model
last_validated
status
kill_condition
notes
```
