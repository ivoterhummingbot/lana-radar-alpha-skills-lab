#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_old_execution_alpha_second_stage as s2  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
SIMS = 900

TESTS = [
    ("no23_mkt30_cd60", "PB05_w15", "TR08_T05_45"),
    ("no21_mkt20", "PB10_w25", "P10_20_L02_45"),
    ("no23_momo40_mkt50_cd60", "PB05_w15", "TR04_T03_20"),
    ("night_mkt20_cd60", "PB10_w25", "TR08_T05_45"),
    ("core_mkt20_cd60", "PB10_w25", "P10_20_L02_45"),
    ("core_mkt20_cd60", "PB10_w25", "TR08_T05_45"),
]


def find_entry(name: str) -> s2.Entry:
    for x in s2.ENTRIES:
        if x.name == name:
            return x
    raise KeyError(name)


def find_exit(name: str) -> s2.Exit:
    for x in s2.EXITS:
        if x.name == name:
            return x
    raise KeyError(name)


def group_by_day(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        out[s2.bjt_day(r)].append(r)
    return dict(sorted(out.items()))


def summarize_day(rows: Sequence[Mapping[str, Any]], univ_rows: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    rand = s2.random_same_ts(univ_rows, rows, seed, SIMS) if rows and univ_rows else {}
    vals = [float(r["pnl"]) for r in rows]
    st = stat(vals)
    return {
        "n": len(rows),
        "avg": st["avg"],
        "sum": st["sum"],
        "sh": st["sharpe_like"],
        "rand_avg_p95": rand.get("avg_p95", 0.0),
        "rand_sum_p95": rand.get("sum_p95", 0.0),
        "edge_avg_p95": st["avg"] - rand.get("avg_p95", 0.0),
        "edge_sum_p95": st["sum"] - rand.get("sum_p95", 0.0),
        "pass_avg": bool(rand and st["avg"] > rand.get("avg_p95", 0.0)),
        "pass_sum": bool(rand and st["sum"] > rand.get("sum_p95", 0.0)),
    }


def main() -> int:
    pools, meta = s2.build_old_pools()
    all_rows = []
    for u, s in pools.values():
        all_rows.extend(u); all_rows.extend(s)
    bars, bars_meta = s2.v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in s2.EXITS) + 45)
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "method": "High-sim daily fresh-forward validation of top old execution-alpha candidates. Same-ts random p95 per BJT entry day; all-taker 8bp.", "settings": {"sims": SIMS, "tests": TESTS}, "meta": {"pools": meta, "bars": bars_meta}, "tests": []}
    lines = ["# Old execution alpha daily validation", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Results", "```text"]
    for idx, (pool_name, entry_name, exit_name) in enumerate(TESTS):
        univ, selected = pools[pool_name]
        es = find_entry(entry_name)
        xs = find_exit(exit_name)
        sel_exec, miss = s2.simulate(selected, bars, es, xs)
        univ_exec, _ = s2.simulate(univ, bars, es, xs)
        overall_rand = s2.random_same_ts(univ_exec, sel_exec, 2026061600 + idx * 1009, SIMS)
        overall = s2.summarize(pool_name, len(selected), sel_exec, overall_rand, miss)
        sel_by_day = group_by_day(sel_exec)
        univ_by_day = group_by_day(univ_exec)
        daily = {d: summarize_day(rows, univ_by_day.get(d, []), 2026061700 + idx * 1009 + j * 37) for j, (d, rows) in enumerate(sel_by_day.items())}
        strict_days = sum(1 for d in daily.values() if d["sum"] > 0 and d["pass_sum"])
        avg_days = sum(1 for d in daily.values() if d["sum"] > 0 and d["pass_avg"])
        row = {"pool": pool_name, "entry": entry_name, "exit": exit_name, "overall": overall, "daily": daily, "strict_days_sum_gt_p95": strict_days, "strict_days_avg_gt_p95": avg_days}
        result["tests"].append(row)
        lines.append(f"{pool_name:<26} {entry_name+'__'+exit_name:<26} n={overall['n']:4d}/{overall['selected_n']:<4d} avg={pct(overall['avg']):>8}/{pct(overall['rand_avg_p95']):>8} edge={pct(overall['edge_avg_p95']):>8} sum={pct(overall['sum']):>8}/{pct(overall['rand_sum_p95']):>8} sh={overall['sh']:5.2f}/{overall['rand_sh_p95']:5.2f} cap5={pct(overall['cap5']['comp']):>8} remT5={pct(overall['remove_top5_avg']):>8} days={overall['positive_days']}/{overall['days']} strict_sum_days={strict_days}/{len(daily)} strict_avg_days={avg_days}/{len(daily)} stop={overall['initial_stop_rate']*100:4.1f}%")
        for d, ds in daily.items():
            lines.append(f"  {d} n={ds['n']:3d} avg={pct(ds['avg']):>8}/{pct(ds['rand_avg_p95']):>8} sum={pct(ds['sum']):>8}/{pct(ds['rand_sum_p95']):>8} pass_avg={ds['pass_avg']} pass_sum={ds['pass_sum']}")
    lines.append("```")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"old-execution-alpha-daily-validate-{ts}.json"
    mp = OUT / f"old-execution-alpha-daily-validate-{ts}.md"
    latest_jp = OUT / "old-execution-alpha-daily-validate-latest.json"
    latest_mp = OUT / "old-execution-alpha-daily-validate-latest.md"
    for p in [jp, latest_jp]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    md = "\n".join(lines) + "\n"
    for p in [mp, latest_mp]:
        p.write_text(md)
    print(jp)
    print(mp)
    print(latest_jp)
    print(latest_mp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
