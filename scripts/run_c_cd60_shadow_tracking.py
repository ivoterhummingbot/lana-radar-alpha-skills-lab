#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import validate_c_cd60_new_radar_gate as gate  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, load_snapshot_rows, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"


def _num(x: Any) -> float:
    try:
        v = float(x or 0.0)
        return v if v == v else 0.0
    except Exception:
        return 0.0


def variant_rows(selected_annotated: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """The three shadow variants Bean approved for forward tracking.

    All filters are causal: new-radar fields are generated from snapshots at or before the old-C signal.
    """
    return {
        "base_C_cd60": list(selected_annotated),
        "risk_gate_BTC15_nonnegative": [
            r for r in selected_annotated if _num(r.get("new_prev30_btc_15m_ret")) >= 0.0
        ],
        "quality_gate_BTC15_nonnegative_AND_prev15_new_mkt_top20": [
            r
            for r in selected_annotated
            if _num(r.get("new_prev30_btc_15m_ret")) >= 0.0 and bool(r.get("new_prev15_mkt_top20"))
        ],
    }


def compact_block(block: Mapping[str, Any]) -> dict[str, Any]:
    o = block["overall"]
    return {
        "signals": block["signals"],
        "n": o["n"],
        "exec_rate": o["exec_rate"],
        "symbols": o["symbols"],
        "avg": o["avg"],
        "rand_avg_p95": o.get("rand_avg_p95", 0.0),
        "edge_avg_p95": o.get("edge_avg_p95", 0.0),
        "sum": o["sum"],
        "rand_sum_p95": o.get("rand_sum_p95", 0.0),
        "edge_sum_p95": o.get("edge_sum_p95", 0.0),
        "sharpe_like": o["sharpe_like"],
        "rand_sh_p95": o.get("rand_sh_p95", 0.0),
        "mdd": o["mdd"],
        "cap5": o["cap5"],
        "cap10": o["cap10"],
        "remove_top5_avg": o["remove_top5_avg"],
        "initial_stop_rate": o["initial_stop_rate"],
        "tp2_rate": o["tp2_rate"],
        "lock_rate": o["lock_rate"],
        "reason_counts": o["reason_counts"],
        "passes": block["passes"],
    }


def write_outputs(result: Mapping[str, Any], prefix: Path) -> tuple[Path, Path]:
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    json_path.write_text(json_text)

    lines: list[str] = []
    lines.append("# C_oldcore_cd60 shadow tracking")
    lines.append("")
    lines.append(f"generated_utc: `{result['generated_utc']}`")
    lines.append("")
    lines.append("Causal fields: old-C signal + new-radar snapshots at/before signal only. Cost: all-taker 8bp. Entry/exit: PB07_w20 + TP0.6/1.2 BE 20m.")
    lines.append("")
    lines.append("## Meta")
    lines.append("```text")
    meta = result["meta"]
    lines.append(f"old_selected={meta['old_selected']} old_universe={meta['old_universe']} new_rows={meta['new_rows']} new_ts={meta['new_ts']}")
    lines.append(f"coverage={meta['coverage']}")
    lines.append(f"bars_symbols={meta['bars_symbols']} bars_errors={meta['bars_errors']}")
    lines.append("```")
    lines.append("")
    lines.append("## Overall")
    lines.append("```text")
    for name, block in result["variants"].items():
        b = block["compact"]
        pass_n = sum(1 for v in b["passes"].values() if v)
        lines.append(
            f"{name:<56} pass={pass_n}/7 sig={b['signals']:3d} n={b['n']:3d} "
            f"avg={pct(b['avg']):>8} rand95={pct(b['rand_avg_p95']):>8} edge={pct(b['edge_avg_p95']):>8} "
            f"sum={pct(b['sum']):>8}/{pct(b['rand_sum_p95']):>8} "
            f"sh={b['sharpe_like']:5.2f}/{b['rand_sh_p95']:5.2f} "
            f"mdd={pct(b['mdd']):>8} cap5={pct(b['cap5']):>8} cap10={pct(b['cap10']):>8} "
            f"remT5={pct(b['remove_top5_avg']):>8} stop={b['initial_stop_rate']*100:5.1f}% "
            f"tp2={b['tp2_rate']*100:5.1f}% lock={b['lock_rate']*100:5.1f}%"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Daily")
    for name, block in result["variants"].items():
        lines.append(f"### {name}")
        lines.append("```text")
        for day, d in block["daily"].items():
            lines.append(
                f"{day} n={d['n']:3d}/{d['selected_n']:<3d} "
                f"avg={pct(d['avg']):>8} rand95={pct(d.get('rand_avg_p95', 0.0)):>8} edge={pct(d.get('edge_avg_p95', 0.0)):>8} "
                f"sum={pct(d['sum']):>8}/{pct(d.get('rand_sum_p95', 0.0)):>8} "
                f"sh={d['sharpe_like']:5.2f}/{d.get('rand_sh_p95', 0.0):5.2f} "
                f"mdd={pct(d['mdd']):>8} remT5={pct(d['remove_top5_avg']):>8} stop={d['initial_stop_rate']*100:5.1f}% "
                f"reasons={d['reason_counts']}"
            )
        lines.append("```")
        lines.append("")
    lines.append("## Promotion/readiness")
    lines.append("```text")
    for name, block in result["variants"].items():
        b = block["compact"]
        daily_values = list(block["daily"].values())
        daily_edges = [d.get("edge_avg_p95", 0.0) for d in daily_values]
        positive_edge_days = sum(1 for e in daily_edges if e > 0)
        lines.append(
            f"{name}: overall_edge={pct(b['edge_avg_p95'])}, positive_edge_days={positive_edge_days}/{len(daily_edges)}, "
            f"remove_top5={pct(b['remove_top5_avg'])}, stop={b['initial_stop_rate']*100:.1f}%, mdd={pct(b['mdd'])}"
        )
    lines.append("```")
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the approved C_oldcore_cd60 shadow variants")
    parser.add_argument("--sims", type=int, default=300)
    parser.add_argument("--latest-prefix", default="output/c-cd60-shadow-tracking-latest")
    args = parser.parse_args()

    gate.v2.SIMS = args.sims
    pools, pool_meta = gate.v2.build_pools()
    universe, selected = pools[gate.POOL]
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    selected_annotated, gate_meta = gate.annotate_with_new_radar(selected, new_rows)
    bars, bars_meta = gate.v2.base.load_bars_by_symbol(universe + selected_annotated, max_minutes=70)
    universe_exec, universe_miss = gate.v2.simulate_combo2(universe, bars, gate.ENTRY, gate.EXIT)

    variants: dict[str, Any] = {}
    for i, (name, rows) in enumerate(variant_rows(selected_annotated).items()):
        block = gate.gate_summary(name, rows, bars, universe_exec, 2026061500 + i * 101)
        variants[name] = {
            "compact": compact_block(block),
            "daily": block["daily"],
            "raw": block,
        }

    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "settings": {
            "sims": args.sims,
            "entry": gate.ENTRY.name,
            "exit": gate.EXIT.name,
            "cost": "all-taker 8bp inside replay helpers",
            "pool": gate.POOL,
        },
        "meta": {
            "old_selected": pool_meta["counts"][gate.POOL]["selected_cap"],
            "old_universe": pool_meta["counts"][gate.POOL]["slim_universe"],
            "old_symbols": pool_meta["counts"][gate.POOL]["symbols"],
            "old_timestamps": pool_meta["counts"][gate.POOL]["timestamps"],
            "new_rows": gate_meta["new_rows"],
            "new_ts": gate_meta["new_ts"],
            "new_complete_end_utc": new_meta.get("complete_end_utc"),
            "coverage": gate_meta["coverage"],
            "bars_symbols": bars_meta.get("symbols"),
            "bars_errors": len(bars_meta.get("errors", {})),
            "universe_exec_rows": len(universe_exec),
            "universe_miss": dict(universe_miss),
        },
        "variants": variants,
    }

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stamped_prefix = OUT / f"c-cd60-shadow-tracking-{ts}"
    latest_prefix = PROJECT_ROOT / args.latest_prefix
    json_path, md_path = write_outputs(result, stamped_prefix)
    latest_json, latest_md = write_outputs(result, latest_prefix)
    print(json_path)
    print(md_path)
    print(latest_json)
    print(latest_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
