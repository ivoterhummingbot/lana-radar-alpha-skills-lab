#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import search_execution_alpha_focused_v2 as v2
from radar_alpha_skills_lab.signal_control import iso, pct

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT = PROJECT_ROOT / "output"

# Same fast-screen pool construction but higher random sims for selected candidates.
v2.SIMS = 200
v2.CAP_PER_TS = 8
v2.SLIM_PER_TS = 25

TOP_TESTS = [
    ("C_oldcore_no23_cd60", v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20), v2.base.ExitSpec("X0_tp06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20)),
    ("C_oldcore_no23_cd60", v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20), v2.base.ExitSpec("X4_tp10_20_L02_45m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.002, sl=-0.015, time_minutes=45)),
    ("C_oldcore_base", v2.Entry2("PB05_w15", "pullback", pullback=0.005, watch_minutes=15), v2.base.ExitSpec("X3_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45)),
    ("C_oldcore_base", v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20), v2.base.ExitSpec("X3_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45)),
    ("A_mkt_core", v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20), v2.base.ExitSpec("X0_tp06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20)),
    ("A_momo_core", v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20), v2.base.ExitSpec("X0_tp06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20)),
]


def main() -> int:
    pools, meta = v2.build_pools()
    keep = {x[0] for x in TOP_TESTS}
    pools = {k: pools[k] for k in pools if k in keep}
    all_rows = []
    for univ, sel in pools.values():
        all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = v2.base.load_bars_by_symbol(all_rows, max_minutes=90)
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "method": "Higher-sim validation of focused-v2 top candidates and best new-radar controls. sims=200, cap8/ts, slim25/ts, all-taker 8bp.", "meta": {"pools": meta, "bars": bars_meta}, "tests": []}
    lines = ["# Focused v2 top-candidate validation", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Results", "```text"]
    for idx, (pool_name, es, xs) in enumerate(TOP_TESTS):
        universe, selected = pools[pool_name]
        sel_exec, miss = v2.simulate_combo2(selected, bars, es, xs)
        univ_exec, _ = v2.simulate_combo2(universe, bars, es, xs)
        summ = v2.base.summarize_rows(sel_exec)
        extra = v2.contribution_extra(sel_exec)
        summ["contribution"] = extra
        rand = v2.random_same_ts(univ_exec, sel_exec, 2026060700 + idx * 101)
        c = summ["contribution"]
        passes = {
            "avg_gt_rand_p95": bool(rand and summ["stats"]["avg"] > rand["avg"]["p95"]),
            "sum_gt_rand_p95": bool(rand and summ["stats"]["sum"] > rand["sum"]["p95"]),
            "sh_gt_rand_p95": bool(rand and summ["stats"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
            "cap5_pos": summ["cap5"]["comp"] > 0,
            "cap10_pos": summ["cap10"]["comp"] > 0,
            "remove_top5_avg_pos": c["remove_top5"]["avg"] >= 0,
            "days_majority_pos": c["positive_avg_days"] >= max(1, math.ceil(c["days"] * 0.5)),
            "initial_stop_le_25pct": c.get("initial_stop_rate", 1.0) <= 0.25,
        }
        row = {
            "pool": pool_name, "combo": f"{es.name}__{xs.name}", "n": summ["n"], "selected_n": len(selected), "exec_rate": summ["n"] / len(selected) if selected else 0.0,
            "avg": summ["stats"]["avg"], "sum": summ["stats"]["sum"], "sh": summ["stats"]["sharpe_like"], "mdd": summ["mdd"],
            "cap5": summ["cap5"]["comp"], "cap10": summ["cap10"]["comp"], "rand": rand, "edge_avg_p95": summ["stats"]["avg"] - rand.get("avg", {}).get("p95", 0.0), "edge_sum_p95": summ["stats"]["sum"] - rand.get("sum", {}).get("p95", 0.0),
            "remove_top5_avg": c["remove_top5"]["avg"], "days": f"{c['positive_avg_days']}/{c['days']}", "initial_stop_rate": c.get("initial_stop_rate", 0.0), "reason_counts": c.get("reason_counts", {}), "missed_entry": dict(miss), "avg_mfe": summ["avg_mfe"], "passes": passes,
        }
        result["tests"].append(row)
        pass_n = sum(1 for v in passes.values() if v)
        lines.append(f"{pool_name:<22} {row['combo']:<34} pass={pass_n}/8 n={row['n']:4d}/{row['selected_n']:<4d} ex={row['exec_rate']*100:5.1f}% avg={pct(row['avg']):>8} rand95={pct(rand['avg']['p95']):>8} edge={pct(row['edge_avg_p95']):>8} sum={pct(row['sum']):>8}/{pct(rand['sum']['p95']):>8} sh={row['sh']:5.2f}/{rand['sharpe_like']['p95']:5.2f} mdd={pct(row['mdd']):>8} cap5={pct(row['cap5']):>8} cap10={pct(row['cap10']):>8} remT5={pct(row['remove_top5_avg']):>8} days={row['days']} stop={row['initial_stop_rate']*100:4.1f}% mfe={pct(row['avg_mfe']):>8} miss={row['missed_entry']} reasons={row['reason_counts']}")
    lines.append("```")
    out = OUT / f"execution-alpha-focused-v2-top-validate-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
