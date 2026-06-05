from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_SOURCE, OUTPUT_DIR, SourceConfig


REQUIRED_CONTRACTS: dict[str, dict[str, set[str]]] = {
    "maker_attention_db": {
        "maker_attn_symbol_scores": {
            "ts",
            "symbol",
            "stage",
            "action_hint",
            "community_heat_score",
            "market_confirmation_score",
            "momentum_confirmation_score",
            "unified_discovery_score",
            "freshness_score",
            "source_quality_score",
            "attention_spread_score",
            "priority_bucket",
            "risk_family",
            "prev5m_ret",
            "prev5m_volume_ratio",
            "prev5m_vwap_distance",
            "prev5m_upper_wick_ratio",
            "prev5m_confirmation_score",
            "prev5m_confirmation_stage",
            "warning_score",
            "fomo_risk_score",
        },
        "maker_attn_market_snapshots": {
            "ts",
            "symbol",
            "raw_symbol",
            "last_price",
            "quote_volume_24h",
            "ret_15m",
            "ret_1h",
            "ret_4h",
            "ret_24h",
            "volume_ratio_1h",
            "distance_from_vwap_4h",
            "distance_from_24h_high",
            "funding_rate",
            "prev5m_ret",
            "prev5m_volume_ratio",
            "prev5m_vwap_distance",
            "prev5m_upper_wick_ratio",
            "prev5m_confirmation_score",
            "prev5m_confirmation_stage",
            "btc_regime_state",
            "btc_regime_score",
            "btc_5m_ret",
            "btc_15m_ret",
            "btc_1h_ret",
            "btc_relative_gate_permission",
            "btc_relative_gate_size_mult",
            "symbol_rel_5m_vs_btc",
        },
    },
    "community_history_db": {
        "lana_community_scores": {
            "ts",
            "symbol",
            "community_heat_score",
            "market_confirmation_score",
            "momentum_confirmation_score",
            "decision_status",
            "recommended_action",
        },
        "community_forward_outcomes": {
            "ts",
            "symbol",
            "decision_status",
            "horizon",
            "entry_price",
            "mfe",
            "mae",
            "close_return",
        },
        "market_snapshots": {
            "ts",
            "btc_ret_1h",
            "btc_ret_4h",
            "btc_ret_24h",
            "alt_breadth_1h",
            "alt_breadth_4h",
            "alt_breadth_24h",
            "hot_count",
            "risk_regime",
        },
    },
}

# New-radar / maker-attention has been decommissioned. Keep its historical
# contract documented for old artifacts, but do not fail the active long-radar
# audit when that local database has been removed.
REQUIRED_DATABASES = {"community_history_db"}


@dataclass(frozen=True)
class TableAudit:
    table: str
    exists: bool
    row_count: int | None
    min_ts: str | None
    max_ts: str | None
    required_columns: list[str]
    present_columns: list[str]
    missing_columns: list[str]

    @property
    def ok(self) -> bool:
        return self.exists and not self.missing_columns


@dataclass(frozen=True)
class DatabaseAudit:
    name: str
    path: str
    required: bool
    exists: bool
    tables: list[TableAudit]

    @property
    def ok(self) -> bool:
        if not self.required and not self.exists:
            return True
        return self.exists and all(t.ok for t in self.tables)


@dataclass(frozen=True)
class InputAudit:
    source_root: str
    output_dir: str
    databases: list[DatabaseAudit]

    @property
    def ok(self) -> bool:
        return all(db.ok for db in self.databases)


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"pragma table_info({table})")]


def _row_count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f"select count(*) from {table}").fetchone()[0])


def _ts_bounds(con: sqlite3.Connection, table: str, columns: set[str]) -> tuple[str | None, str | None]:
    if "ts" not in columns:
        return None, None
    row = con.execute(f"select min(ts), max(ts) from {table}").fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def audit_database(name: str, path: Path, tables: dict[str, set[str]]) -> DatabaseAudit:
    required = name in REQUIRED_DATABASES
    if not path.exists():
        return DatabaseAudit(name=name, path=str(path), required=required, exists=False, tables=[])

    table_audits: list[TableAudit] = []
    with _open_ro(path) as con:
        for table, required in tables.items():
            exists = _table_exists(con, table)
            if not exists:
                table_audits.append(
                    TableAudit(
                        table=table,
                        exists=False,
                        row_count=None,
                        min_ts=None,
                        max_ts=None,
                        required_columns=sorted(required),
                        present_columns=[],
                        missing_columns=sorted(required),
                    )
                )
                continue

            present = set(_table_columns(con, table))
            row_count = _row_count(con, table)
            min_ts, max_ts = _ts_bounds(con, table, present)
            table_audits.append(
                TableAudit(
                    table=table,
                    exists=True,
                    row_count=row_count,
                    min_ts=min_ts,
                    max_ts=max_ts,
                    required_columns=sorted(required),
                    present_columns=sorted(present),
                    missing_columns=sorted(required - present),
                )
            )

    return DatabaseAudit(name=name, path=str(path), required=required, exists=True, tables=table_audits)


def run_input_audit(source: SourceConfig = DEFAULT_SOURCE) -> InputAudit:
    db_paths = {
        "maker_attention_db": source.maker_attention_db,
        "community_history_db": source.community_history_db,
    }
    audits = [
        audit_database(name, db_paths[name], contracts)
        for name, contracts in REQUIRED_CONTRACTS.items()
    ]
    return InputAudit(source_root=str(source.source_root), output_dir=str(OUTPUT_DIR), databases=audits)


def audit_to_dict(audit: InputAudit) -> dict[str, Any]:
    raw = asdict(audit)
    raw["ok"] = audit.ok
    for db_obj, db_raw in zip(audit.databases, raw["databases"]):
        db_raw["ok"] = db_obj.ok
        for table_obj, table_raw in zip(db_obj.tables, db_raw["tables"]):
            table_raw["ok"] = table_obj.ok
    return raw


def render_markdown(audit: InputAudit) -> str:
    lines = [
        "# Read-only Input Audit",
        "",
        f"Source root: `{audit.source_root}`",
        f"Lab output dir: `{audit.output_dir}`",
        f"Overall status: `{'OK' if audit.ok else 'FAIL'}`",
        "",
        "## Databases",
        "",
    ]
    for db in audit.databases:
        lines.extend(
            [
                f"### {db.name}",
                "",
                f"Path: `{db.path}`",
                f"Required: `{db.required}`",
                f"Exists: `{db.exists}`",
                f"Status: `{'OK' if db.ok else 'FAIL'}`",
                "",
            ]
        )
        for table in db.tables:
            lines.extend(
                [
                    f"- `{table.table}`",
                    f"  - exists: `{table.exists}`",
                    f"  - rows: `{table.row_count}`",
                    f"  - ts: `{table.min_ts}` → `{table.max_ts}`",
                    f"  - required columns: `{len(table.required_columns)}`",
                    f"  - missing columns: `{', '.join(table.missing_columns) if table.missing_columns else 'none'}`",
                    f"  - status: `{'OK' if table.ok else 'FAIL'}`",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_audit_outputs(audit: InputAudit, output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "readonly-input-audit.json"
    md_path = output_dir / "readonly-input-audit.md"
    json_path.write_text(json.dumps(audit_to_dict(audit), indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(render_markdown(audit))
    return json_path, md_path
