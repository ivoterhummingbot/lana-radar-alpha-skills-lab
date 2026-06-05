#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys

import search_execution_alpha_focused_v2 as v2
from radar_alpha_skills_lab.signal_control import iso, pct

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)
OUT = PROJECT_ROOT / "output"

# Fast focused screen: enough to identify direction, not exhaustive.
v2.SIMS = 25
v2.CAP_PER_TS = 8
v2.SLIM_PER_TS = 25
v2.ENTRIES = [
    v2.Entry2("N1_next1m", "next1m"),
    v2.Entry2("PB03_w20", "pullback", pullback=0.003, watch_minutes=20),
    v2.Entry2("PB05_w15", "pullback", pullback=0.005, watch_minutes=15),
    v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20),
]
v2.EXITS = [
    v2.base.ExitSpec("X0_tp06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20),
    v2.base.ExitSpec("X1_tp08_16_BE_30m", tp1=0.008, tp2=0.016, tp1_size=0.5, lock=0.0, sl=-0.012, time_minutes=30),
    v2.base.ExitSpec("X3_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45),
    v2.base.ExitSpec("X4_tp10_20_L02_45m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.002, sl=-0.015, time_minutes=45),
]
KEEP_POOLS = {"A_mkt_core", "A_entry_core", "A_momo_core", "A_blend_core", "C_oldcore_base", "C_oldcore_no23_cd60"}


def main() -> int:
    pools, meta = v2.build_pools()
    pools = {k: pools[k] for k in pools if k in KEEP_POOLS}
    meta["counts"] = {k: meta["counts"][k] for k in pools}
    all_rows = []
    for univ, sel in pools.values():
        all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in v2.EXITS) + 40)
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "method": "FAST focused v2 screen: selected new-radar MFE pools + best old-C variants, cap8/ts, slim25/ts, 4 entries x 4 exits, all-taker 8bp, same-ts execution random sims25.", "meta": {"pools": meta, "bars": bars_meta}, "pools": {}, "leaderboard": []}
    combo_idx = 0
    for pool_name, (universe, selected) in pools.items():
        result["pools"][pool_name] = {"universe_n": len(universe), "selected_n": len(selected), "combos": {}}
        for es in v2.ENTRIES:
            for xs in v2.EXITS:
                combo_idx += 1
                key = f"{es.name}__{xs.name}"
                sel_exec, miss = v2.simulate_combo2(selected, bars, es, xs)
                if len(sel_exec) < 20:
                    continue
                univ_exec, _ = v2.simulate_combo2(universe, bars, es, xs)
                summ = v2.base.summarize_rows(sel_exec)
                extra = v2.contribution_extra(sel_exec)
                summ["contribution"] = extra
                rand = v2.random_same_ts(univ_exec, sel_exec, 2026060600 + combo_idx * 53 + len(pool_name))
                c = summ["contribution"]
                passes = {
                    "avg_gt_rand_p95": bool(rand and summ["stats"]["avg"] > rand["avg"]["p95"]),
                    "sum_gt_rand_p95": bool(rand and summ["stats"]["sum"] > rand["sum"]["p95"]),
                    "sh_gt_rand_p95": bool(rand and summ["stats"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
                    "cap5_pos": bool(summ["cap5"]["comp"] > 0),
                    "cap10_pos": bool(summ["cap10"]["comp"] > 0),
                    "remove_top5_avg_pos": bool(c["remove_top5"]["avg"] >= 0),
                    "days_majority_pos": bool(c["positive_avg_days"] >= max(1, math.ceil(c["days"] * 0.5))),
                    "initial_stop_le_25pct": bool(c.get("initial_stop_rate", 1.0) <= 0.25),
                }
                row = {
                    "pool": pool_name, "combo": key, "n": summ["n"], "selected_n": len(selected),
                    "exec_rate": summ["n"] / len(selected) if selected else 0.0,
                    "avg": summ["stats"]["avg"], "sum": summ["stats"]["sum"], "sh": summ["stats"]["sharpe_like"], "mdd": summ["mdd"],
                    "cap5": summ["cap5"]["comp"], "cap10": summ["cap10"]["comp"],
                    "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0,
                    "rand_sum_p95": rand.get("sum", {}).get("p95", 0.0) if rand else 0.0,
                    "rand_sh_p95": rand.get("sharpe_like", {}).get("p95", 0.0) if rand else 0.0,
                    "edge_avg_p95": summ["stats"]["avg"] - (rand.get("avg", {}).get("p95", 0.0) if rand else 0.0),
                    "remove_top5_avg": c["remove_top5"]["avg"], "days": f"{c['positive_avg_days']}/{c['days']}",
                    "initial_stop_rate": c.get("initial_stop_rate", 0.0), "reason_counts": c.get("reason_counts", {}),
                    "missed_entry": dict(miss), "avg_mfe": summ["avg_mfe"], "avg_capture": summ["avg_capture_ratio"], "passes": passes,
                }
                result["pools"][pool_name]["combos"][key] = {"entry": es.__dict__, "exit": xs.__dict__, "selected": summ, "random_same_ts": rand, "passes": passes, "missed_entry": dict(miss), "universe_exec_n": len(univ_exec)}
                result["leaderboard"].append(row)
    result["leaderboard"] = sorted(result["leaderboard"], key=lambda r: (sum(1 for v in r["passes"].values() if v), r["edge_avg_p95"], r["avg"], r["remove_top5_avg"]), reverse=True)
    lines = ["# Fast focused execution alpha screen v2", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Coverage", "```text"]
    lines.append(f"new_window={meta['new_window_utc'][0]} -> {meta['new_window_utc'][1]}")
    for k, c in meta["counts"].items():
        lines.append(f"{k}: raw_universe={c['raw_universe']} slim_universe={c['slim_universe']} selected_raw={c['selected_raw']} selected_cap={c['selected_cap']} symbols={c['symbols']} ts={c['timestamps']}")
    lines.append(f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}")
    lines.extend(["```", "", "## Leaderboard", "", "```text"])
    for r in result["leaderboard"][:50]:
        pass_n = sum(1 for v in r["passes"].values() if v)
        lines.append(f"{r['pool']:<22} {r['combo']:<34} pass={pass_n}/8 n={r['n']:4d}/{r['selected_n']:<4d} ex={r['exec_rate']*100:5.1f}% avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']):>8} cap10={pct(r['cap10']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['days']} stop={r['initial_stop_rate']*100:4.1f}% mfe={pct(r['avg_mfe']):>8} reasons={r['reason_counts']}")
    lines.append("```")
    lines.extend(["", "## Best by pool", ""])
    for pool_name in pools:
        lines.append(f"### {pool_name}")
        lines.append("```text")
        for r in [x for x in result["leaderboard"] if x["pool"] == pool_name][:6]:
            pass_n = sum(1 for v in r["passes"].values() if v)
            lines.append(f"{r['combo']:<34} pass={pass_n}/8 n={r['n']:4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8} sh={r['sh']:5.2f} mdd={pct(r['mdd']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['days']} stop={r['initial_stop_rate']*100:4.1f}% miss={r['missed_entry']}")
        lines.append("```")
        lines.append("")
    out = OUT / f"execution-alpha-focused-v2-fast-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
