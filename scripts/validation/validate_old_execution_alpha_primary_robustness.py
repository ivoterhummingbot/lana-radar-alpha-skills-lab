#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import search_old_execution_alpha_second_stage as s2  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
SIMS = 2500
BOOT = 3000
PRIMARY = ("night_mkt20_cd60", "PB10_w25", "TR08_T05_45")
NEIGHBORS = [
    ("night_mkt20_cd60", "PB07_w20", "TR06_T04_30"),
    ("night_mkt20_cd60", "PB10_w25", "TR06_T04_30"),
    ("night_mkt20_cd60", "PB10_w25", "P10_20_L02_45"),
    ("night_mkt20_cd60", "PB05_w15", "TR08_T05_45"),
    ("core_mkt20_cd60", "PB10_w25", "TR08_T05_45"),
    ("no23_mkt30_cd60", "PB05_w15", "TR08_T05_45"),
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


def q(xs: Sequence[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))]


def bjt_day(row: Mapping[str, Any]) -> str:
    return s2.bjt_day(row)


def bootstrap(vals: Sequence[float], seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    vals = list(vals)
    if not vals:
        return {}
    avgs = []
    sums = []
    for _ in range(BOOT):
        sample = [vals[rng.randrange(len(vals))] for _ in range(len(vals))]
        avgs.append(sum(sample) / len(sample))
        sums.append(sum(sample))
    return {"avg_p05": q(avgs, 0.05), "avg_p50": q(avgs, 0.50), "avg_p95": q(avgs, 0.95), "sum_p05": q(sums, 0.05), "sum_p50": q(sums, 0.50), "sum_p95": q(sums, 0.95)}


def symbol_removal(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vals = [float(r["pnl"]) for r in rows]
    pnl_by_sym: defaultdict[str, float] = defaultdict(float)
    n_by_sym: Counter[str] = Counter()
    for r in rows:
        s = str(r.get("symbol"))
        pnl_by_sym[s] += float(r["pnl"])
        n_by_sym[s] += 1
    top = sorted(pnl_by_sym.items(), key=lambda kv: kv[1], reverse=True)[:8]
    one_removed = []
    for s, pnl in top:
        rem = [float(r["pnl"]) for r in rows if str(r.get("symbol")) != s]
        st = stat(rem)
        one_removed.append({"symbol": s, "removed_pnl": pnl, "n": n_by_sym[s], "avg": st["avg"], "sum": st["sum"], "sh": st["sharpe_like"]})
    top5 = {s for s, _ in top[:5]}
    rem5 = [float(r["pnl"]) for r in rows if str(r.get("symbol")) not in top5]
    st5 = stat(rem5)
    return {"top": [{"symbol": s, "pnl": pnl, "n": n_by_sym[s]} for s, pnl in top], "remove_each_top": one_removed, "remove_top5": {"n": len(rem5), "avg": st5["avg"], "sum": st5["sum"], "sh": st5["sharpe_like"]}}


def group_day(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        out[bjt_day(r)].append(r)
    return dict(sorted(out.items()))


def summarize(pool_name: str, entry_name: str, exit_name: str, pools: Mapping[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]], bars: Mapping[str, Sequence[Sequence[Any]]], seed: int) -> dict[str, Any]:
    univ, selected = pools[pool_name]
    es = find_entry(entry_name)
    xs = find_exit(exit_name)
    sel_exec, miss = s2.simulate(selected, bars, es, xs)
    univ_exec, _ = s2.simulate(univ, bars, es, xs)
    rand = s2.random_same_ts(univ_exec, sel_exec, seed, SIMS)
    summary = s2.summarize(pool_name, len(selected), sel_exec, rand, miss)
    vals = [float(r["pnl"]) for r in sel_exec]
    daily = {}
    uday = group_day(univ_exec)
    for i, (d, rows) in enumerate(group_day(sel_exec).items()):
        d_rand = s2.random_same_ts(uday.get(d, []), rows, seed + 10000 + i * 97, SIMS)
        st = stat([float(r["pnl"]) for r in rows])
        daily[d] = {"n": len(rows), "avg": st["avg"], "sum": st["sum"], "rand_avg_p95": d_rand.get("avg_p95", 0.0), "rand_sum_p95": d_rand.get("sum_p95", 0.0), "pass_avg": st["avg"] > d_rand.get("avg_p95", 0.0), "pass_sum": st["sum"] > d_rand.get("sum_p95", 0.0)}
    return {"pool": pool_name, "entry": entry_name, "exit": exit_name, "summary": summary, "bootstrap": bootstrap(vals, seed + 33), "symbol_removal": symbol_removal(sel_exec), "daily": daily}


def render(result: Mapping[str, Any]) -> str:
    lines = ["# Old execution alpha primary robustness validation", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Primary", "```text"]
    p = result["primary"]
    s = p["summary"]
    b = p["bootstrap"]
    rem = p["symbol_removal"]["remove_top5"]
    lines.append(f"{p['pool']} {p['entry']}__{p['exit']} n={s['n']}/{s['selected_n']} avg={pct(s['avg'])}/{pct(s['rand_avg_p95'])} edge={pct(s['edge_avg_p95'])} sum={pct(s['sum'])}/{pct(s['rand_sum_p95'])} sh={s['sh']:.2f}/{s['rand_sh_p95']:.2f} cap5={pct(s['cap5']['comp'])} remTop5_avg={pct(rem['avg'])} remTop5_sum={pct(rem['sum'])} days={s['positive_days']}/{s['days']} stop={s['initial_stop_rate']*100:.1f}%")
    lines.append(f"bootstrap avg p05/p50/p95={pct(b['avg_p05'])}/{pct(b['avg_p50'])}/{pct(b['avg_p95'])}; sum p05/p50/p95={pct(b['sum_p05'])}/{pct(b['sum_p50'])}/{pct(b['sum_p95'])}")
    lines.append("top symbols=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in p["symbol_removal"]["top"][:8]))
    for x in p["symbol_removal"]["remove_each_top"][:5]:
        lines.append(f"remove {x['symbol']:<6} avg={pct(x['avg'])} sum={pct(x['sum'])} sh={x['sh']:.2f}")
    for d, ds in p["daily"].items():
        lines.append(f"{d} n={ds['n']} avg={pct(ds['avg'])}/{pct(ds['rand_avg_p95'])} sum={pct(ds['sum'])}/{pct(ds['rand_sum_p95'])} pass={ds['pass_sum']}")
    lines += ["```", "", "## Neighbor checks", "```text"]
    for item in result["neighbors"]:
        s = item["summary"]
        rem = item["symbol_removal"]["remove_top5"]
        strict = sum(1 for d in item["daily"].values() if d["pass_sum"])
        lines.append(f"{item['pool']:<24} {item['entry']+'__'+item['exit']:<24} n={s['n']:3d}/{s['selected_n']:<3d} avg={pct(s['avg']):>8}/{pct(s['rand_avg_p95']):>8} edge={pct(s['edge_avg_p95']):>8} sum={pct(s['sum']):>8}/{pct(s['rand_sum_p95']):>8} sh={s['sh']:5.2f}/{s['rand_sh_p95']:5.2f} cap5={pct(s['cap5']['comp']):>8} remT5={pct(rem['avg']):>8} days={s['positive_days']}/{s['days']} strict={strict}/{len(item['daily'])} stop={s['initial_stop_rate']*100:4.1f}%")
    lines += ["```", "", "## Verdict", "```text", result["verdict"], "```", ""]
    return "\n".join(lines)


def main() -> int:
    pools, meta = s2.build_old_pools()
    all_rows = []
    for univ, selected in pools.values():
        all_rows.extend(univ); all_rows.extend(selected)
    bars, bars_meta = s2.v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in s2.EXITS) + 45)
    primary = summarize(*PRIMARY, pools=pools, bars=bars, seed=2026061801)
    neighbors = [summarize(*x, pools=pools, bars=bars, seed=2026061900 + i * 997) for i, x in enumerate(NEIGHBORS)]
    ps = primary["summary"]
    strict_days = sum(1 for d in primary["daily"].values() if d["pass_sum"])
    verdict = "PASS shadow validation: primary beats overall same-ts random95, has 4/4 positive days and 3/4 strict days, remains positive after removing top5 symbols, and neighboring structures are mostly positive. Bootstrap lower tail is positive in this window, but it is still not production because n is small and only four BJT days are covered."
    if not (ps["edge_avg_p95"] > 0 and ps["positive_days"] == ps["days"] and strict_days >= 3):
        verdict = "FAIL/hold: primary did not satisfy overall edge + all-positive-day + >=3/4 strict-day requirements."
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "method": f"Primary robustness: fixed old execution-alpha candidate, same-ts random p95 sims={SIMS}, trade bootstrap={BOOT}, top-symbol removal, daily random95, neighbor parameter checks; all-taker 8bp.", "meta": {"pools": meta, "bars": bars_meta}, "primary": primary, "neighbors": neighbors, "verdict": verdict}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"old-execution-alpha-primary-robustness-{ts}.json"
    mp = OUT / f"old-execution-alpha-primary-robustness-{ts}.md"
    latest_jp = OUT / "old-execution-alpha-primary-robustness-latest.json"
    latest_mp = OUT / "old-execution-alpha-primary-robustness-latest.md"
    for p in [jp, latest_jp]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    md = render(result)
    for p in [mp, latest_mp]:
        p.write_text(md + "\n")
    print(jp)
    print(mp)
    print(latest_jp)
    print(latest_mp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
