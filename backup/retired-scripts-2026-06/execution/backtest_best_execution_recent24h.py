#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
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

import search_execution_alpha_narrow as base  # noqa: E402
import validate_full_data_first_layer_discovery_fast_clean as clean_base  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import candidate_top_fraction_by_ts  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

BJ = timezone(timedelta(hours=8))
OUT = PROJECT_ROOT / "output"

BEST_ENTRY = base.EntrySpec("E2_pullback05", "pullback", pullback=0.005)
BEST_EXIT = base.ExitSpec(
    "X2_tp12_24_L01_45m",
    tp1=0.012,
    tp2=0.024,
    tp1_size=0.5,
    lock=0.001,
    sl=-0.018,
    time_minutes=45,
)
CAP_PER_TS = 10
RANDOM_SIMS = 100


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def cap_by_ts(rows: Sequence[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, group in sorted(by.items()):
        out.extend(
            sorted(
                group,
                key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))),
            )[:cap]
        )
    return out


def slim_universe_for_selected(universe: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], per_ts: int = 40) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in universe:
        by[r["ts_dt"]].append(dict(r))
    out: dict[tuple[datetime, str], dict[str, Any]] = {}
    for ts, group in by.items():
        ranked = sorted(group, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))))[:per_ts]
        for r in ranked:
            out[(ts, str(r.get("symbol")))] = dict(r)
    for r in selected:
        out[(r["ts_dt"], str(r.get("symbol")))] = dict(r)
    return list(out.values())


def day_hour_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_day: defaultdict[str, list[float]] = defaultdict(list)
    by_hour: defaultdict[int, list[float]] = defaultdict(list)
    for r in rows:
        pnl = float(r["pnl"])
        by_day[str(r.get("date_bjt"))].append(pnl)
        by_hour[int(r.get("hour_bjt") or 0)].append(pnl)
    return {
        "day": {d: {"n": len(v), "sum": sum(v), "avg": sum(v)/len(v)} for d, v in sorted(by_day.items())},
        "hour_bjt": {str(h): {"n": len(v), "sum": sum(v), "avg": sum(v)/len(v)} for h, v in sorted(by_hour.items())},
    }


def compact_trade(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "signal_bjt": r["ts_dt"].astimezone(BJ).strftime("%m-%d %H:%M:%S"),
        "entry_bjt": datetime.fromisoformat(str(r["entry_dt"])).astimezone(BJ).strftime("%m-%d %H:%M:%S"),
        "symbol": r.get("symbol"),
        "pnl": float(r.get("pnl") or 0.0),
        "mfe": float(r.get("mfe") or 0.0),
        "mae": float(r.get("mae") or 0.0),
        "reason": r.get("reason"),
        "score": _num(r.get("market_confirmation_score")),
        "final_score": _num(r.get("final_score")),
        "entry_price": float(r.get("entry_price") or 0.0),
    }


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(hours=24)
    rows, old_meta = clean_base.load_old_signal_scores()
    old_core = [dict(r) for r in rows if str(r.get("session")) == "core_night"]
    c_all = candidate_top_fraction_by_ts(old_core, "market_confirmation_score", 0.20)
    universe_24 = [dict(r) for r in old_core if start_utc <= r["ts_dt"] <= now_utc]
    selected_raw_24 = [dict(r) for r in c_all if start_utc <= r["ts_dt"] <= now_utc]
    selected_24 = cap_by_ts(selected_raw_24, CAP_PER_TS)
    universe_24_slim = slim_universe_for_selected(universe_24, selected_24, per_ts=40)

    all_for_bars = list(universe_24_slim) + list(selected_24)
    bars, bars_meta = base.load_bars_by_symbol(all_for_bars, max_minutes=BEST_EXIT.time_minutes + 35)
    selected_exec = base.simulate_combo(selected_24, bars, BEST_ENTRY, BEST_EXIT)
    universe_exec = base.simulate_combo(universe_24_slim, bars, BEST_ENTRY, BEST_EXIT)
    missed = Counter()
    for r in selected_24:
        b = bars.get(str(r.get("raw_symbol")), [])
        _dt, _px, er = base.decide_entry(b, r["ts_dt"], BEST_ENTRY)
        if _dt is None:
            missed[er] += 1
    base.SIMS = RANDOM_SIMS
    rand = base.random_same_ts_execution(universe_exec, selected_exec, 2026060424)
    summ = base.summarize_rows(selected_exec) if selected_exec else {
        "n": 0,
        "stats": stat([]),
        "mdd": 0.0,
        "avg_mfe": 0.0,
        "avg_capture_ratio": 0.0,
        "cap5": base.cap_portfolio([], 5),
        "cap10": base.cap_portfolio([], 10),
        "contribution": base.contribution([]),
    }
    c = summ["contribution"]
    reasons = Counter(str(r.get("reason")) for r in selected_exec)
    wins = sum(1 for r in selected_exec if float(r["pnl"]) > 0)
    losses = sum(1 for r in selected_exec if float(r["pnl"]) < 0)
    top_trades = sorted(selected_exec, key=lambda r: float(r["pnl"]), reverse=True)[:15]
    bottom_trades = sorted(selected_exec, key=lambda r: float(r["pnl"]))[:15]
    result = {
        "generated_utc": iso(now_utc),
        "window": {
            "start_utc": iso(start_utc),
            "end_utc": iso(now_utc),
            "start_bjt": start_utc.astimezone(BJ).isoformat(),
            "end_bjt": now_utc.astimezone(BJ).isoformat(),
        },
        "method": "Recent-24h backtest of best pilot candidate: P4_C_oldcore_mfe, top20% old_core by timestamp, cap10 selected per timestamp, entry pullback -0.5% within 15m, TP1 +1.2% half, TP2 +2.4%, TP1 lock +0.1%, SL -1.8%, time stop 45m, all-taker 8bp, stop-first 1m replay.",
        "old_meta": old_meta,
        "counts": {
            "old_core_universe_24h": len(universe_24),
            "selected_raw_24h": len(selected_raw_24),
            "selected_cap10_24h": len(selected_24),
            "executed_trades": len(selected_exec),
            "universe_exec_for_random": len(universe_exec),
            "missed_entry": dict(missed),
        },
        "bars": bars_meta,
        "entry": BEST_ENTRY.__dict__,
        "exit": BEST_EXIT.__dict__,
        "summary": summ,
        "random_same_ts": rand,
        "win_loss": {"wins": wins, "losses": losses, "flat": len(selected_exec)-wins-losses, "win_rate": wins/len(selected_exec) if selected_exec else 0.0},
        "reason_counts": dict(reasons),
        "day_hour": day_hour_stats(selected_exec),
        "top_trades": [compact_trade(r) for r in top_trades],
        "bottom_trades": [compact_trade(r) for r in bottom_trades],
    }

    lines = [
        "# Best execution alpha recent 24h backtest",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"window_bjt: `{result['window']['start_bjt']}` -> `{result['window']['end_bjt']}`",
        "",
        result["method"],
        "",
        "## Coverage",
        "```text",
        f"old_core_universe_24h={len(universe_24)} selected_raw_24h={len(selected_raw_24)} selected_cap10_24h={len(selected_24)} executed_trades={len(selected_exec)} universe_exec_for_random={len(universe_exec)}",
        f"missed_entry={dict(missed)}",
        f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}",
        "```",
        "",
        "## Summary",
        "```text",
    ]
    st = summ["stats"]
    lines.append(f"n={summ['n']} avg={pct(st['avg'])} sum={pct(st['sum'])} sharpe_like={st['sharpe_like']:.2f} mdd={pct(summ['mdd'])}")
    lines.append(f"win_rate={wins}/{len(selected_exec)} ({wins/len(selected_exec)*100 if selected_exec else 0:.1f}%) avg_mfe={pct(summ['avg_mfe'])} capture={summ['avg_capture_ratio']:.2f}")
    lines.append(f"cap5_comp={pct(summ['cap5']['comp'])} cap10_comp={pct(summ['cap10']['comp'])}")
    if rand:
        lines.append(f"same_ts_random_avg p50={pct(rand['avg']['p50'])} p95={pct(rand['avg']['p95'])}; selected_avg_edge_vs_p95={pct(st['avg']-rand['avg']['p95'])}")
        lines.append(f"same_ts_random_sum p50={pct(rand['sum']['p50'])} p95={pct(rand['sum']['p95'])}; selected_sum_edge_vs_p95={pct(st['sum']-rand['sum']['p95'])}")
        lines.append(f"same_ts_random_sharpe p50={rand['sharpe_like']['p50']:.2f} p95={rand['sharpe_like']['p95']:.2f}")
    lines.append(f"reason_counts={dict(reasons)}")
    lines.append(f"remove_top5_avg={pct(c['remove_top5']['avg'])} top_symbols={c['top_symbols'][:8]}")
    lines.append("```")
    lines.extend(["", "## BJT day/hour", "```text"])
    for d, v in result["day_hour"]["day"].items():
        lines.append(f"day {d}: n={v['n']} sum={pct(v['sum'])} avg={pct(v['avg'])}")
    for h, v in result["day_hour"]["hour_bjt"].items():
        lines.append(f"hour {int(h):02d}: n={v['n']} sum={pct(v['sum'])} avg={pct(v['avg'])}")
    lines.append("```")
    lines.extend(["", "## Best trades", "```text"])
    for t in result["top_trades"][:12]:
        lines.append(f"{t['signal_bjt']} {str(t['symbol']):<18} pnl={pct(t['pnl']):>8} mfe={pct(t['mfe']):>8} mae={pct(t['mae']):>8} {t['reason']}")
    lines.extend(["```", "", "## Worst trades", "```text"])
    for t in result["bottom_trades"][:12]:
        lines.append(f"{t['signal_bjt']} {str(t['symbol']):<18} pnl={pct(t['pnl']):>8} mfe={pct(t['mfe']):>8} mae={pct(t['mae']):>8} {t['reason']}")
    lines.append("```")

    out = OUT / f"best-execution-recent24h-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
