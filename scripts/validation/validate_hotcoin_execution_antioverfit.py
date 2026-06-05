#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import search_hotcoin_execution_proxy as proxy  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
EXIT = next(x for x in proxy.EXITS if x[0] == "H24_sl60")
TARGET_POOLS = [
    "core_mkt10_cd60",
    "night_mkt10_cd60",
    "day_high_mkt10_cd60",
    "all_mkt10_cd60",
    "watch_hot_mkt10_cd60",
    "core_mkt20_cd60",
    "night_mkt20_cd60",
]
SIMS = 1200
DAY_SIMS = 600


def bjt_day(r):
    return r["ts_dt"].astimezone(BJ).date().isoformat()


def sim_subset(rows, days):
    dset = set(days)
    return [r for r in rows if bjt_day(r) in dset]


def calc_remove(rows, topn=5):
    bysym = defaultdict(float); ns = Counter()
    for r in rows:
        s = str(r.get("symbol")); bysym[s] += float(r["pnl"]); ns[s] += 1
    top = sorted(bysym.items(), key=lambda kv: kv[1], reverse=True)[:topn]
    exclude = {s for s, _ in top}
    rem = [float(r["pnl"]) for r in rows if str(r.get("symbol")) not in exclude]
    rst = stat(rem)
    return {
        "top": [{"symbol": s, "pnl": v, "n": ns[s]} for s, v in top],
        "rem_avg": rst["avg"],
        "rem_sum": rst["sum"],
        "rem_n": len(rem),
    }


def daywise(us, ss):
    uby = defaultdict(list); sby = defaultdict(list)
    for r in us: uby[bjt_day(r)].append(r)
    for r in ss: sby[bjt_day(r)].append(r)
    rows = []
    for i, d in enumerate(sorted(sby)):
        vals = [float(r["pnl"]) for r in sby[d]]
        st = stat(vals)
        rand = proxy.rand_same_ts(uby[d], sby[d], 2026060800 + i * 31)
        rows.append({
            "date": d,
            "n": len(vals),
            "avg": st["avg"],
            "sum": st["sum"],
            "rand_avg_p95": rand["avg_p95"],
            "rand_sum_p95": rand["sum_p95"],
            "pass_avg": st["avg"] > rand["avg_p95"],
            "pass_sum": st["sum"] > rand["sum_p95"],
            "stop_rate": sum(1 for r in sby[d] if r.get("reason") == "hard_sl") / max(1, len(sby[d])),
        })
    return rows


def summarize_period(name, us, ss, seed):
    if not ss:
        return {"period": name, "n": 0}
    rand = proxy.rand_same_ts(us, ss, seed)
    vals = [float(r["pnl"]) for r in ss]
    st = stat(vals)
    rem = calc_remove(ss, 5)
    days = sorted({bjt_day(r) for r in ss})
    return {
        "period": name,
        "days": days,
        "n": len(ss),
        "avg": st["avg"],
        "sum": st["sum"],
        "sh": st["sharpe_like"],
        "rand_avg_p95": rand["avg_p95"],
        "rand_sum_p95": rand["sum_p95"],
        "rand_sh_p95": rand["sh_p95"],
        "edge_avg": st["avg"] - rand["avg_p95"],
        "edge_sum": st["sum"] - rand["sum_p95"],
        "stop_rate": sum(1 for r in ss if r.get("reason") == "hard_sl") / max(1, len(ss)),
        "remove_top5": rem,
        "positive_days": sum(1 for d in days if sum(float(r["pnl"]) for r in ss if bjt_day(r) == d) > 0),
    }


def main():
    old_sims = proxy.SIMS
    proxy.SIMS = SIMS
    pools, meta = proxy.build()
    all_target_selected = []
    for p in TARGET_POOLS:
        if p in pools:
            all_target_selected.extend(pools[p][1])
    all_days = sorted({bjt_day(r) for r in all_target_selected})
    recent3 = all_days[-3:]
    prior = all_days[:-3]
    results = []
    for idx, p in enumerate(TARGET_POOLS):
        if p not in pools:
            continue
        univ, sel = pools[p]
        us_all = proxy.simulate(univ, EXIT)
        ss_all = proxy.simulate(sel, EXIT)
        periods = []
        periods.append(summarize_period("full", us_all, ss_all, 2026060900 + idx * 101))
        periods.append(summarize_period("prior_ex_recent3", sim_subset(us_all, prior), sim_subset(ss_all, prior), 2026061000 + idx * 101))
        periods.append(summarize_period("recent3", sim_subset(us_all, recent3), sim_subset(ss_all, recent3), 2026061100 + idx * 101))
        days = daywise(us_all, ss_all)
        strict_days = sum(1 for d in days if d["pass_sum"])
        pos_days = sum(1 for d in days if d["sum"] > 0)
        results.append({
            "pool": p,
            "periods": periods,
            "daywise": days,
            "strict_days": strict_days,
            "positive_days": pos_days,
            "total_days": len(days),
        })
    proxy.SIMS = old_sims
    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "rule": "mkt*_cd60 + H24_sl60, stop-first proxy, all-taker 8bp",
        "all_days": all_days,
        "recent3": recent3,
        "prior_ex_recent3": prior,
        "sims": SIMS,
        "day_sims": DAY_SIMS,
        "meta": meta,
        "results": results,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"hotcoin-execution-antioverfit-{ts}.json"
    mp = OUT / f"hotcoin-execution-antioverfit-{ts}.md"
    lj = OUT / "hotcoin-execution-antioverfit-latest.json"
    lm = OUT / "hotcoin-execution-antioverfit-latest.md"
    md = render(result)
    for p in [jp, lj]: p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    for p in [mp, lm]: p.write_text(md + "\n")
    print(jp); print(mp); print(lj); print(lm)


def fmt_period(x):
    if x.get("n", 0) == 0:
        return f"{x['period']}: n=0"
    rem = x["remove_top5"]
    return (
        f"{x['period']:<16} n={x['n']:4d} avg={pct(x['avg']):>8}/{pct(x['rand_avg_p95']):>8} "
        f"edge={pct(x['edge_avg']):>8} sum={pct(x['sum']):>9}/{pct(x['rand_sum_p95']):>9} "
        f"edgeSum={pct(x['edge_sum']):>9} sh={x['sh']:5.2f}/{x['rand_sh_p95']:5.2f} "
        f"remT5={pct(rem['rem_avg']):>8}/{pct(rem['rem_sum']):>9} "
        f"posDays={x['positive_days']}/{len(x['days'])} stop={x['stop_rate']*100:4.1f}%"
    )


def render(result):
    lines = [
        "# Hotcoin execution anti-overfit validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"rule: `{result['rule']}`",
        f"recent3: `{', '.join(result['recent3'])}`",
        f"prior_ex_recent3: `{', '.join(result['prior_ex_recent3'])}`",
        "",
        "Anti-overfit criteria: full window positive edge vs same-ts random95; prior window excluding recent3 still positive edge; remove-top5 remains positive; daily positives broad; neighboring pools agree.",
        "",
        "## Pool summaries",
        "```text",
    ]
    for r in result["results"]:
        lines.append(f"[{r['pool']}] strictDays={r['strict_days']}/{r['total_days']} positiveDays={r['positive_days']}/{r['total_days']}")
        for p in r["periods"]:
            lines.append("  " + fmt_period(p))
        top = r["periods"][0].get("remove_top5", {}).get("top", []) if r["periods"] else []
        if top:
            lines.append("  full_top=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in top))
    lines += ["```", "", "## Daily strict pass table", "```text"]
    for r in result["results"][:3]:
        lines.append(f"[{r['pool']}]")
        for d in r["daywise"]:
            lines.append(
                f"  {d['date']} n={d['n']:3d} avg={pct(d['avg']):>8}/{pct(d['rand_avg_p95']):>8} "
                f"sum={pct(d['sum']):>9}/{pct(d['rand_sum_p95']):>9} pass={d['pass_sum']} stop={d['stop_rate']*100:4.1f}%"
            )
    lines += ["```", "", "## Verdict", "```text"]
    primary = next((r for r in result["results"] if r["pool"] == "core_mkt10_cd60"), None)
    if primary:
        full, prior, recent = primary["periods"]
        ok = (
            full["edge_avg"] > 0 and prior["edge_avg"] > 0 and recent["edge_avg"] > 0 and
            full["remove_top5"]["rem_avg"] > 0 and prior["remove_top5"]["rem_avg"] > 0 and
            primary["positive_days"] == primary["total_days"]
        )
        if ok:
            lines.append("NOT a pure recent-window overfit under proxy tests: core_mkt10_cd60 + H24_sl60 passes full window, prior excluding recent3, recent3, remove-top5, and has all days positive. Still proxy-only; OHLC replay/fresh-forward required before production.")
        else:
            lines.append("Overfit risk remains: primary fails at least one anti-overfit gate.")
    lines += ["```", ""]
    return "\n".join(lines)

if __name__ == "__main__":
    main()
