#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import search_hotcoin_execution_proxy as proxy  # noqa: E402
import validate_hotcoin_execution_recent24h as recent  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"
WINDOW_HOURS = 48
SIMS = 2000
POOLS = recent.POOLS
EXIT = recent.EXIT


def render(result):
    lines = [
        "# Hotcoin H24_SL60 recent-48h validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"signal_window_hours: `{result['window_hours']}`",
        f"window_utc: `{result['window_start_utc']} -> {result['window_end_utc']}`",
        f"window_bjt: `{result['window_start_bjt']} -> {result['window_end_bjt']}`",
        f"rule: `{result['rule']}`",
        f"sims: `{result['sims']}`",
        "",
        "Uses latest complete 24h outcome rows only; signal window is 48h; conservative stop-first proxy; same-ts random p95; all-taker 8bp.",
        "",
        "## Results",
        "```text",
    ]
    for r in result["results"]:
        mt = r.get("max_trade") or {}
        mn = r.get("min_trade") or {}
        lines.append(
            f"{r['pool']:<22} n={r['n']:3d}/{r['universe_n']:<4d} "
            f"avg={pct(r['avg']):>8}/{pct(r['random_avg_p95']):>8} edge={pct(r['edge_avg']):>8} "
            f"sum={pct(r['sum']):>9}/{pct(r['random_sum_p95']):>9} edgeSum={pct(r['edge_sum']):>9} "
            f"sharpe={r['sharpe_like']:5.2f}/{r['random_sharpe_p95']:5.2f} "
            f"win={r['win_rate']*100:4.1f}% stop={r['stop_rate']*100:4.1f}% "
            f"max={mt.get('symbol')} {pct(mt.get('pnl',0)):>8} min={mn.get('symbol')} {pct(mn.get('pnl',0)):>8} "
            f"remT5={pct(r['remove_top5_avg']):>8}/{pct(r['remove_top5_sum']):>8} ddSum={pct(r['drawdown_sum_path']):>8}"
        )
        lines.append("  reasons=" + json.dumps(r["reasons"], ensure_ascii=False))
        lines.append("  top_symbols=" + ", ".join(f"{x['symbol']} sum={pct(x['sum'])}/n={x['n']}/max={pct(x['max'])}" for x in r["top_symbols"][:8]))
        if mt:
            lines.append(f"  max_trade={mt['symbol']} pnl={pct(mt['pnl'])} return24={pct(mt['return_24h'])} mfe24={pct(mt['mfe_24h'])} mae24={pct(mt['mae_24h'])} ts_bjt={mt['ts_bjt']} reason={mt['reason']}")
        if mn:
            lines.append(f"  min_trade={mn['symbol']} pnl={pct(mn['pnl'])} return24={pct(mn['return_24h'])} mfe24={pct(mn['mfe_24h'])} mae24={pct(mn['mae_24h'])} ts_bjt={mn['ts_bjt']} reason={mn['reason']}")
    lines += ["```", "", "## Verdict", "```text"]
    primary = next((x for x in result["results"] if x["pool"] == "core_mkt10_cd60"), None)
    day_high = next((x for x in result["results"] if x["pool"] == "day_high_mkt10_cd60"), None)
    if primary and primary["n"] > 0 and primary["edge_avg"] > 0 and primary["sharpe_like"] > primary["random_sharpe_p95"]:
        lines.append("PASS recent-48h proxy validation for primary core_mkt10_cd60 + H24_sl60: positive edge and Sharpe above same-ts random95.")
    elif primary and primary["n"] > 0:
        lines.append("HOLD/WEAK recent-48h proxy validation for primary: positive/negative details above; do not upgrade based on this window.")
    else:
        lines.append("No primary trades in latest complete 48h signal window.")
    if primary and primary.get("remove_top5_avg", 0.0) <= 0:
        lines.append("Caution: primary remove-top5 average is non-positive; concentration risk remains high.")
    if day_high and day_high.get("stop_rate", 1.0) < primary.get("stop_rate", 1.0):
        lines.append("Day-high sibling has lower stop rate than primary in this 48h window; keep it as cleaner risk-control sibling.")
    lines += ["```", ""]
    return "\n".join(lines)


def main() -> int:
    old_sims = proxy.SIMS
    try:
        proxy.SIMS = SIMS
        pools, meta = proxy.build()
        target_rows = []
        for p in POOLS:
            if p in pools:
                target_rows.extend(pools[p][1])
        if not target_rows:
            raise SystemExit("no target rows")
        end = max(r["ts_dt"] for r in target_rows)
        start = end - timedelta(hours=WINDOW_HOURS)
        results = []
        for i, p in enumerate(POOLS):
            if p not in pools:
                continue
            univ, sel = pools[p]
            univ_w = recent.window_filter(univ, start, end)
            sel_w = recent.window_filter(sel, start, end)
            results.append(recent.summarize_pool(p, univ_w, sel_w, 2026064800 + i * 111))
    finally:
        proxy.SIMS = old_sims

    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "rule": "market top10 + cd60 + H24_sl60 (-6% hard stop, 24h hold, all-taker 8bp)",
        "window_hours": WINDOW_HOURS,
        "window_start_utc": iso(start),
        "window_end_utc": iso(end),
        "window_start_bjt": recent.bjt(start),
        "window_end_bjt": recent.bjt(end),
        "meta": meta,
        "sims": SIMS,
        "results": results,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"hotcoin-execution-recent48h-h24sl60-{ts}.json"
    mp = OUT / f"hotcoin-execution-recent48h-h24sl60-{ts}.md"
    lj = OUT / "hotcoin-execution-recent48h-h24sl60-latest.json"
    lm = OUT / "hotcoin-execution-recent48h-h24sl60-latest.md"
    md = render(result)
    for p in [jp, lj]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    for p in [mp, lm]:
        p.write_text(md + "\n")
    print(jp)
    print(mp)
    print(lj)
    print(lm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
