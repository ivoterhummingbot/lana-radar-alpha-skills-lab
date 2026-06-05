#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_hotcoin_execution_proxy as proxy  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
POOL_NAMES = ["day_high_mkt10_cd60", "core_mkt10_cd60", "night_mkt10_cd60"]
EXIT = next(x for x in proxy.EXITS if x[0] == "H24_sl60")
SIMS = 1500


def bjt_day(r):
    return r["ts_dt"].astimezone(BJ).date().isoformat()


def day_breakdown(rows, univ_rows):
    by = defaultdict(list)
    uby = defaultdict(list)
    for r in rows:
        by[bjt_day(r)].append(r)
    for r in univ_rows:
        uby[bjt_day(r)].append(r)
    out = []
    for d in sorted(by):
        vals = [float(r["pnl"]) for r in by[d]]
        st = proxy.stat(vals)
        # day-level same-ts random within the same day's universe/selected timestamps
        rand = proxy.rand_same_ts(uby[d], by[d], 2026060700 + int(d.replace('-', '')),)
        out.append({
            "date_bjt": d,
            "n": len(vals),
            "avg": st["avg"],
            "sum": st["sum"],
            "sh": st["sharpe_like"],
            "rand_avg_p95": rand["avg_p95"],
            "rand_sum_p95": rand["sum_p95"],
            "pass_sum": st["sum"] > rand["sum_p95"],
            "pass_avg": st["avg"] > rand["avg_p95"],
            "stop_rate": sum(1 for r in by[d] if r.get("reason") == "hard_sl") / max(1, len(by[d])),
        })
    return out


def main() -> int:
    old_sims = proxy.SIMS
    proxy.SIMS = SIMS
    pools, meta = proxy.build()
    # Use latest 3 BJT dates that have complete 24h outcome in selected target pools.
    all_target_rows = []
    for p in POOL_NAMES:
        if p in pools:
            all_target_rows.extend(pools[p][1])
    dates = sorted({bjt_day(r) for r in all_target_rows})
    recent3 = dates[-3:]
    results = []
    for p in POOL_NAMES:
        if p not in pools:
            continue
        univ, sel = pools[p]
        univ3 = [r for r in univ if bjt_day(r) in recent3]
        sel3 = [r for r in sel if bjt_day(r) in recent3]
        us = proxy.simulate(univ3, EXIT)
        ss = proxy.simulate(sel3, EXIT)
        rand = proxy.rand_same_ts(us, ss, 2026060711 + len(results) * 101)
        summary = proxy.summarize(p, EXIT, len(sel3), ss, rand)
        summary["day_breakdown"] = day_breakdown(ss, us)
        results.append(summary)
    proxy.SIMS = old_sims
    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "rule": "mkt10_cd60 + H24_sl60 (24h hold, -6% hard stop, all-taker 8bp)",
        "recent3_bjt_dates": recent3,
        "meta": meta,
        "sims": SIMS,
        "results": results,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"hotcoin-execution-recent3-h24sl60-{ts}.json"
    mp = OUT / f"hotcoin-execution-recent3-h24sl60-{ts}.md"
    lj = OUT / "hotcoin-execution-recent3-h24sl60-latest.json"
    lm = OUT / "hotcoin-execution-recent3-h24sl60-latest.md"
    md = render(result)
    for p in [jp, lj]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    for p in [mp, lm]:
        p.write_text(md + "\n")
    print(jp); print(mp); print(lj); print(lm)
    return 0


def render(result) -> str:
    lines = [
        "# Hotcoin H24_SL60 recent-3 validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"rule: `{result['rule']}`",
        f"recent3_bjt_dates: `{', '.join(result['recent3_bjt_dates'])}`",
        "",
        "Uses complete 24h old-radar outcome rows only; conservative stop-first proxy; same-ts random p95; all-taker 8bp.",
        "",
        "## Results",
        "```text",
    ]
    for r in result["results"]:
        lines.append(
            f"{r['pool']:<22} n={r['n']:3d}/{r['selected_n']:<3d} "
            f"avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg']):>8} "
            f"sum={pct(r['sum']):>9}/{pct(r['rand_sum_p95']):>9} edgeSum={pct(r['edge_sum']):>9} "
            f"sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} "
            f"cap5={pct(r['cap5']['comp']):>9} remT5={pct(r['rem_top5_avg']):>8}/{pct(r['rem_top5_sum']):>8} "
            f"days={r['pos_days']}/{r['days']} stop={r['stop_rate']*100:4.1f}%"
        )
        lines.append("  top=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in r['top_symbols']))
        for d in r["day_breakdown"]:
            lines.append(
                f"  {d['date_bjt']} n={d['n']:2d} avg={pct(d['avg']):>8}/{pct(d['rand_avg_p95']):>8} "
                f"sum={pct(d['sum']):>9}/{pct(d['rand_sum_p95']):>9} "
                f"pass={d['pass_sum']} stop={d['stop_rate']*100:4.1f}%"
            )
    lines += ["```", "", "## Verdict", "```text"]
    passes = [r for r in result["results"] if r["edge_avg"] > 0 and r["rem_top5_avg"] > 0 and r["pos_days"] == r["days"]]
    if passes:
        best = max(passes, key=lambda r: (r["edge_avg"], r["rem_top5_avg"]))
        lines.append(f"PASS recent-3 proxy validation. Best: {best['pool']} + H24_sl60. Continue to OHLC replay/fresh-forward, not production yet.")
    else:
        lines.append("HOLD/FAIL recent-3 proxy validation: no pool passed avg edge + remove-top + all positive days together.")
    lines += ["```", ""]
    return "\n".join(lines)

if __name__ == "__main__":
    raise SystemExit(main())
