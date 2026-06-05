#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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

import search_hotcoin_execution_scheme as h  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"
# Minimal targeted candidates based on discovery evidence: market top hotcoin pools,
# immediate or modest pullback entries, longer exits to capture 4h/24h continuation.
TARGETS = [
    ("night_mkt20_cd60", "N5", "TR20_T10_360"),
    ("night_mkt20_cd60", "PB05_w30", "TR20_T10_360"),
    ("core_mkt20_cd60", "N5", "TR20_T10_360"),
    ("core_mkt20_cd60", "PB05_w30", "TR20_T10_360"),
    ("day_high_mkt20_cd60", "N5", "TR20_T10_360"),
    ("wait_entry_mkt20_cd60", "N5", "TR20_T10_360"),
]
SIMS = 100


def render(meta, results):
    lines = [
        "# Targeted hotcoin execution validation",
        "",
        f"generated_utc: `{iso(datetime.now(timezone.utc))}`",
        "",
        "This is not a large grid search. It directly validates hotcoin-discovery execution ideas: market-score hot pools, N5/PB entries, 4h hold or 6h trailing, same-ts random p95, all-taker 8bp.",
        "",
        "## Meta", "```text", json.dumps(meta, ensure_ascii=False, indent=2, default=str), "```", "",
        "## Results", "```text",
    ]
    for r in results:
        lines.append(f"{r['pool']:<24} {r['entry']+'__'+r['exit']:<22} n={r['n']:4d}/{r['selected_n']:<4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg']):>8} sum={pct(r['sum']):>9}/{pct(r['rand_sum_p95']):>9} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} cap5={pct(r['cap5']['comp']):>9} remT5={pct(r['rem_top5_avg']):>8}/{pct(r['rem_top5_sum']):>8} days={r['pos_days']}/{r['days']} stop={r['stop_rate']*100:4.1f}% exec={r['exec_rate']*100:4.1f}%")
        lines.append("  reasons=" + json.dumps(r['reasons'], ensure_ascii=False) + " top=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in r['top_symbols']))
    lines += ["```", "", "## Verdict", "```text"]
    passing = [r for r in results if r['edge_avg'] > 0 and r['rem_top5_avg'] > 0 and r['pos_days'] >= max(1, int(r['days'] * 0.60)) and r['stop_rate'] <= 0.35]
    if passing:
        b = passing[0]
        lines.append(f"Best targeted hotcoin execution candidate: {b['pool']} + {b['entry']} + {b['exit']}. It passes average same-ts random p95 and remains positive after top-symbol removal. Treat as shadow candidate, then run higher-sim/fresh-forward.")
    else:
        lines.append("No targeted hotcoin execution candidate passed the full-window gates. Discovery/continuation remains valid, but executable capture still needs stronger regime gate or different exit geometry.")
    lines += ["```", ""]
    return "\n".join(lines)


def main():
    pools, meta = h.build_pools()
    target_names = {p for p, _e, _x in TARGETS}
    all_rows=[]
    for name, (univ, sel) in pools.items():
        if name not in target_names:
            continue
        all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = h.s2.v2.base.load_bars_by_symbol(all_rows, max_minutes=450)
    meta['bars'] = bars_meta
    meta['sims'] = SIMS
    results=[]
    for i, (pool, en, ex) in enumerate(TARGETS):
        if pool not in pools:
            continue
        e = next(x for x in h.ENTRIES if x.name == en)
        x = next(y for y in h.EXITS if y.name == ex)
        univ, sel = pools[pool]
        r = h.run_combo(pool, univ, sel, bars, e, x, SIMS, 2026060500+i*137)
        if r:
            results.append(r)
    results.sort(key=lambda r: (r['edge_avg'] > 0, r['rem_top5_avg'] > 0, r['pos_days']/max(1, r['days']), r['edge_avg'], r['score']), reverse=True)
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    jp = OUT / f"hotcoin-execution-targeted-{ts}.json"
    mp = OUT / f"hotcoin-execution-targeted-{ts}.md"
    lj = OUT / "hotcoin-execution-targeted-latest.json"
    lm = OUT / "hotcoin-execution-targeted-latest.md"
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "meta": meta, "results": results}
    md = render(meta, results)
    for p in [jp, lj]: p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+"\n")
    for p in [mp, lm]: p.write_text(md+"\n")
    print(jp); print(mp); print(lj); print(lm)

if __name__ == '__main__':
    main()
