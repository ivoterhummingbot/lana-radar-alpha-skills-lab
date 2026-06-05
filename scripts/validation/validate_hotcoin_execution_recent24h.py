#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

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
EXIT = next(x for x in proxy.EXITS if x[0] == "H24_sl60")
POOLS = ["core_mkt10_cd60", "night_mkt10_cd60", "day_high_mkt10_cd60", "all_mkt10_cd60"]
SIMS = 2000
BJ = timezone(timedelta(hours=8))


def bjt(x):
    return x.astimezone(BJ).isoformat()


def window_filter(rows, start, end):
    return [r for r in rows if start <= r["ts_dt"] <= end]


def max_trade(rows):
    if not rows:
        return None
    r = max(rows, key=lambda x: float(x["pnl"]))
    return trade_view(r)


def min_trade(rows):
    if not rows:
        return None
    r = min(rows, key=lambda x: float(x["pnl"]))
    return trade_view(r)


def trade_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(r.get("symbol")),
        "ts_utc": iso(r["ts_dt"]),
        "ts_bjt": bjt(r["ts_dt"]),
        "pnl": float(r["pnl"]),
        "reason": str(r.get("reason")),
        "return_24h": float(r.get("return_24h") or 0.0),
        "mfe_24h": float(r.get("mfe_24h") or 0.0),
        "mae_24h": float(r.get("mae_24h") or 0.0),
        "market_confirmation_score": float(r.get("market_confirmation_score") or 0.0),
        "momentum_confirmation_score": float(r.get("momentum_confirmation_score") or 0.0),
        "session": str(r.get("session")),
        "decision_status": str(r.get("decision_status")),
    }


def symbol_stats(rows):
    by = defaultdict(list)
    for r in rows:
        by[str(r.get("symbol"))].append(float(r["pnl"]))
    out = []
    for s, vals in by.items():
        st = stat(vals)
        out.append({"symbol": s, "n": len(vals), "sum": st["sum"], "avg": st["avg"], "max": max(vals), "min": min(vals)})
    out.sort(key=lambda x: x["sum"], reverse=True)
    return out


def drawdown(vals):
    eq = 0.0; peak = 0.0; mdd = 0.0
    for v in vals:
        eq += v
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def summarize_pool(name, univ, sel, seed):
    us = proxy.simulate(univ, EXIT)
    ss = proxy.simulate(sel, EXIT)
    rand = proxy.rand_same_ts(us, ss, seed)
    vals = [float(r["pnl"]) for r in ss]
    st = stat(vals)
    symbols = symbol_stats(ss)
    rem_ex = {x["symbol"] for x in symbols[:5]}
    rem_vals = [float(r["pnl"]) for r in ss if str(r.get("symbol")) not in rem_ex]
    rem_st = stat(rem_vals)
    return {
        "pool": name,
        "n": len(ss),
        "universe_n": len(us),
        "avg": st["avg"],
        "sum": st["sum"],
        "sharpe_like": st["sharpe_like"],
        "random_avg_p95": rand["avg_p95"],
        "random_sum_p95": rand["sum_p95"],
        "random_sharpe_p95": rand["sh_p95"],
        "edge_avg": st["avg"] - rand["avg_p95"],
        "edge_sum": st["sum"] - rand["sum_p95"],
        "stop_rate": sum(1 for r in ss if str(r.get("reason")) == "hard_sl") / max(1, len(ss)),
        "win_rate": sum(1 for v in vals if v > 0) / max(1, len(vals)),
        "max_trade": max_trade(ss),
        "min_trade": min_trade(ss),
        "top_symbols": symbols[:10],
        "remove_top5_avg": rem_st["avg"],
        "remove_top5_sum": rem_st["sum"],
        "drawdown_sum_path": drawdown(vals),
        "reasons": dict(Counter(str(r.get("reason")) for r in ss)),
    }


def render(result):
    lines = [
        "# Hotcoin H24_SL60 recent-24h validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"window_utc: `{result['window_start_utc']} -> {result['window_end_utc']}`",
        f"window_bjt: `{result['window_start_bjt']} -> {result['window_end_bjt']}`",
        f"rule: `{result['rule']}`",
        f"sims: `{result['sims']}`",
        "",
        "Uses latest complete 24h outcome rows only; conservative stop-first proxy; same-ts random p95; all-taker 8bp.",
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
    if primary and primary["n"] > 0 and primary["edge_avg"] > 0 and primary["sharpe_like"] > primary["random_sharpe_p95"]:
        lines.append("PASS recent-24h proxy validation for primary core_mkt10_cd60 + H24_sl60: positive edge and Sharpe above same-ts random95.")
    elif primary and primary["n"] > 0:
        lines.append("HOLD/WEAK recent-24h proxy validation for primary: positive/negative details above; do not upgrade based on this window.")
    else:
        lines.append("No primary trades in latest complete 24h window.")
    lines += ["```", ""]
    return "\n".join(lines)


def main() -> int:
    old_sims = proxy.SIMS
    proxy.SIMS = SIMS
    pools, meta = proxy.build()
    target_rows = []
    for p in POOLS:
        if p in pools:
            target_rows.extend(pools[p][1])
    if not target_rows:
        raise SystemExit("no target rows")
    end = max(r["ts_dt"] for r in target_rows)
    start = end - timedelta(hours=24)
    results = []
    for i, p in enumerate(POOLS):
        if p not in pools:
            continue
        univ, sel = pools[p]
        univ_w = window_filter(univ, start, end)
        sel_w = window_filter(sel, start, end)
        results.append(summarize_pool(p, univ_w, sel_w, 2026061200 + i * 111))
    proxy.SIMS = old_sims
    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "rule": "market top10 + cd60 + H24_sl60 (-6% hard stop, 24h hold, all-taker 8bp)",
        "window_start_utc": iso(start),
        "window_end_utc": iso(end),
        "window_start_bjt": bjt(start),
        "window_end_bjt": bjt(end),
        "meta": meta,
        "sims": SIMS,
        "results": results,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"hotcoin-execution-recent24h-h24sl60-{ts}.json"
    mp = OUT / f"hotcoin-execution-recent24h-h24sl60-{ts}.md"
    lj = OUT / "hotcoin-execution-recent24h-h24sl60-latest.json"
    lm = OUT / "hotcoin-execution-recent24h-h24sl60-latest.md"
    md = render(result)
    for p in [jp, lj]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    for p in [mp, lm]:
        p.write_text(md + "\n")
    print(jp); print(mp); print(lj); print(lm)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
