#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_execution_alpha_narrow as base  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

# Fast pilot: capacity-focused, not final exhaustive search.
base.SIMS = 30
PILOT_ENTRIES = [
    base.EntrySpec("E1_next5m", "next5m"),
    base.EntrySpec("E2_pullback05", "pullback", pullback=0.005),
    base.EntrySpec("E4_pb05_break5m", "pullback_break", pullback=0.005, break_minutes=5),
    base.EntrySpec("E5_skip5mPump1_next5m", "next5m", skip_pump=0.010),
]
PILOT_EXITS = [
    base.ExitSpec("X1_tp10_20_BE_30m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.0, sl=-0.015, time_minutes=30),
    base.ExitSpec("X2_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45),
    base.ExitSpec("X5_tp08_16_BE_30m", tp1=0.008, tp2=0.016, tp1_size=0.5, lock=0.0, sl=-0.012, time_minutes=30),
]


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def cap_by_ts(rows: Sequence[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    by: defaultdict[Any, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        out.extend(sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))))[:cap])
    return out


def best_reason_line(row: Mapping[str, Any]) -> str:
    return ",".join(f"{k}:{v}" for k, v in sorted(row.get("reason_counts", {}).items()))


def main() -> int:
    pools, meta = base.load_pools()
    # Cap selected to realistic capacity first-pass; keep universe slim as built by base.
    pools = {k: (univ, cap_by_ts(sel, 10)) for k, (univ, sel) in pools.items()}
    all_rows = []
    for univ, sel in pools.values():
        all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = base.load_bars_by_symbol(all_rows, max_minutes=80)
    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "FAST PILOT execution alpha search: fixed discovery pools, selected capped top10 per timestamp, 4 entry x 3 exit, 1m replay, all-taker 8bp, same-ts execution random over slim top40 universe. Directional screen only, not final exhaustive validation.",
        "meta": {"pools": meta, "bars": bars_meta, "selected_cap_per_ts": 10, "sims": base.SIMS},
        "leaderboard": [],
        "pools": {},
    }
    for pool_name, (universe, selected) in pools.items():
        result["pools"][pool_name] = {"universe_n": len(universe), "selected_n": len(selected), "combos": {}}
        idx = 0
        for es in PILOT_ENTRIES:
            for xs in PILOT_EXITS:
                idx += 1
                key = f"{es.name}__{xs.name}"
                sel_exec = base.simulate_combo(selected, bars, es, xs)
                univ_exec = base.simulate_combo(universe, bars, es, xs)
                if len(sel_exec) < 20:
                    continue
                summ = base.summarize_rows(sel_exec)
                rand = base.random_same_ts_execution(univ_exec, sel_exec, 202606041200 + idx * 31 + len(pool_name))
                c = summ["contribution"]
                passes = {
                    "avg_gt_rand_p95": bool(rand and summ["stats"]["avg"] > rand["avg"]["p95"]),
                    "sum_gt_rand_p95": bool(rand and summ["stats"]["sum"] > rand["sum"]["p95"]),
                    "sh_gt_rand_p95": bool(rand and summ["stats"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
                    "cap5_pos": bool(summ["cap5"]["comp"] > 0),
                    "cap10_pos": bool(summ["cap10"]["comp"] > 0),
                    "remove_top5_avg_pos": bool(c["remove_top5"]["avg"] >= 0),
                    "days_majority_pos": bool(c["positive_avg_days"] >= max(1, math.ceil(c["days"] * 0.5))),
                }
                row = {
                    "pool": pool_name,
                    "combo": key,
                    "n": summ["n"],
                    "avg": summ["stats"]["avg"],
                    "sum": summ["stats"]["sum"],
                    "sh": summ["stats"]["sharpe_like"],
                    "mdd": summ["mdd"],
                    "cap5": summ["cap5"]["comp"],
                    "cap10": summ["cap10"]["comp"],
                    "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0,
                    "rand_sum_p95": rand.get("sum", {}).get("p95", 0.0) if rand else 0.0,
                    "remove_top5_avg": c["remove_top5"]["avg"],
                    "days": f"{c['positive_avg_days']}/{c['days']}",
                    "reason_counts": c["reason_counts"],
                    "avg_capture": summ["avg_capture_ratio"],
                    "avg_mfe": summ["avg_mfe"],
                    "passes": passes,
                }
                result["pools"][pool_name]["combos"][key] = {"selected": summ, "random_same_ts": rand, "passes": passes}
                result["leaderboard"].append(row)
    result["leaderboard"] = sorted(result["leaderboard"], key=lambda r: (sum(1 for v in r["passes"].values() if v), r["avg"], r["remove_top5_avg"]), reverse=True)
    lines = ["# Execution alpha fast pilot", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Coverage", "```text"]
    lines.append(f"overlap={meta['overlap_start_utc']} -> {meta['overlap_end_utc']}")
    for k, c in meta["counts"].items():
        lines.append(f"{k}: raw_universe={c['raw_universe']} slim_universe={c['slim_universe']} selected_raw={c['selected']} selected_cap10={len(pools[k][1])}")
    lines.append(f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}")
    lines.extend(["```", "", "## Leaderboard", "", "```text"])
    for r in result["leaderboard"][:40]:
        pass_n = sum(1 for v in r["passes"].values() if v)
        lines.append(f"{r['pool']:<22} {r['combo']:<42} pass={pass_n}/7 n={r['n']:4d} avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:6.2f} mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']):>8} cap10={pct(r['cap10']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['days']} capRatio={r['avg_capture']:.2f} mfe={pct(r['avg_mfe']):>8} reasons={best_reason_line(r)}")
    lines.append("```")
    out = PROJECT_ROOT / "output" / f"execution-alpha-fast-pilot-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
