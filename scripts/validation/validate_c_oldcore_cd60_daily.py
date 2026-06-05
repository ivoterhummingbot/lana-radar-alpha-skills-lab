#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
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

import search_execution_alpha_focused_v2 as v2  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct, stat  # noqa: E402

BJ = timezone(timedelta(hours=8))
OUT = PROJECT_ROOT / "output"

# Match the latest C_oldcore_cd60 benchmark setting used against new-radar exit search.
v2.SIMS = 300
v2.CAP_PER_TS = 8
v2.SLIM_PER_TS = 12
POOL = "C_oldcore_cd60"
ENTRY = v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20)
EXIT = v2.base.ExitSpec("P06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20)


def bjt_day_of(row: Mapping[str, Any]) -> str:
    dt = row.get("entry_dt") or row.get("ts_dt")
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ).date().isoformat()


def q(xs: Sequence[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))]


def random_same_ts_exec(universe_exec: Sequence[Mapping[str, Any]], selected_exec: Sequence[Mapping[str, Any]], seed: int, sims: int = 300) -> dict[str, Any]:
    if not selected_exec or len(selected_exec) >= len(universe_exec):
        return {}
    u_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    s_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in universe_exec:
        if r.get("pnl") is not None:
            u_by[r["ts_dt"]].append(r)
    for r in selected_exec:
        s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    shs: list[float] = []
    mdds: list[float] = []
    for _ in range(sims):
        picked_rows: list[Mapping[str, Any]] = []
        for ts, sg in s_by.items():
            pool = u_by.get(ts, [])
            if not pool:
                continue
            pool = sorted(pool, key=lambda r: (-float(r.get("market_confirmation_score") or 0.0), str(r.get("symbol"))))[: v2.SLIM_PER_TS]
            n = len(sg)
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            picked_rows.extend(sample)
        vals = [float(r["pnl"]) for r in picked_rows]
        st = stat(vals)
        avgs.append(st["avg"])
        sums.append(st["sum"])
        shs.append(st["sharpe_like"])
        mdds.append(v2.base.max_drawdown(vals))
    return {
        "sims": sims,
        "avg": {"p50": q(avgs, 0.50), "p95": q(avgs, 0.95)},
        "sum": {"p50": q(sums, 0.50), "p95": q(sums, 0.95)},
        "sharpe_like": {"p50": q(shs, 0.50), "p95": q(shs, 0.95)},
        "mdd": {"p50": q(mdds, 0.50), "p05": q(mdds, 0.05)},
    }


def cap_portfolio(rows: Sequence[Mapping[str, Any]], cap: int) -> dict[str, float]:
    by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(r)
    picked: list[float] = []
    for _ts, group in sorted(by.items()):
        ranked = sorted(group, key=lambda r: (-float(r.get("market_confirmation_score") or 0.0), -float(r.get("final_score") or 0.0), str(r.get("symbol"))))[:cap]
        picked.extend(float(r["pnl"]) for r in ranked)
    comp = 1.0
    for v in picked:
        comp *= 1.0 + v
    return {"n": len(picked), "sum": sum(picked), "avg": sum(picked) / len(picked) if picked else 0.0, "comp": comp - 1.0}


def summarize(rows: Sequence[Mapping[str, Any]], selected_n: int, rand: Mapping[str, Any] | None = None) -> dict[str, Any]:
    vals = [float(r["pnl"]) for r in rows]
    st = stat(vals)
    reasons = Counter(str(r.get("reason")) for r in rows)
    syms = Counter(str(r.get("symbol")) for r in rows)
    pnl_by_sym: defaultdict[str, float] = defaultdict(float)
    n_by_sym: Counter[str] = Counter()
    for r in rows:
        s = str(r.get("symbol"))
        pnl_by_sym[s] += float(r["pnl"])
        n_by_sym[s] += 1
    top_pnl = sorted(pnl_by_sym.items(), key=lambda kv: kv[1], reverse=True)[:5]
    bottom_pnl = sorted(pnl_by_sym.items(), key=lambda kv: kv[1])[:5]
    top_by_count = syms.most_common(8)
    remove_top5_vals = vals[:]
    top5_syms = {s for s, _ in top_pnl}
    remove_top5_vals = [float(r["pnl"]) for r in rows if str(r.get("symbol")) not in top5_syms]
    cap5 = cap_portfolio(rows, 5)
    cap10 = cap_portfolio(rows, 10)
    win = sum(1 for x in vals if x > 0)
    mfe_vals = [float(r.get("mfe", 0.0) or 0.0) for r in rows]
    mae_vals = [float(r.get("mae", 0.0) or 0.0) for r in rows]
    out = {
        "selected_n": selected_n,
        "n": len(rows),
        "exec_rate": len(rows) / selected_n if selected_n else 0.0,
        "symbols": len(set(str(r.get("symbol")) for r in rows)),
        "avg": st["avg"],
        "sum": st["sum"],
        "sharpe_like": st["sharpe_like"],
        "mdd": v2.base.max_drawdown(vals),
        "win_rate": win / len(vals) if vals else 0.0,
        "cap5": cap5["comp"],
        "cap10": cap10["comp"],
        "remove_top5_avg": stat(remove_top5_vals)["avg"] if remove_top5_vals else 0.0,
        "initial_stop_rate": reasons.get("initial_stop", 0) / len(rows) if rows else 0.0,
        "tp2_rate": reasons.get("tp2_after_tp1", 0) / len(rows) if rows else 0.0,
        "lock_rate": reasons.get("lock_after_tp1", 0) / len(rows) if rows else 0.0,
        "time_rate": (reasons.get("time_exit", 0) + reasons.get("tp1_then_time", 0)) / len(rows) if rows else 0.0,
        "reason_counts": dict(reasons),
        "avg_mfe": sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0.0,
        "avg_mae": sum(mae_vals) / len(mae_vals) if mae_vals else 0.0,
        "top_symbols_by_count": top_by_count,
        "top_symbols_by_pnl": [{"symbol": s, "pnl": v, "n": n_by_sym[s]} for s, v in top_pnl],
        "bottom_symbols_by_pnl": [{"symbol": s, "pnl": v, "n": n_by_sym[s]} for s, v in bottom_pnl],
    }
    if rand:
        out.update({
            "rand_avg_p50": rand["avg"]["p50"],
            "rand_avg_p95": rand["avg"]["p95"],
            "rand_sum_p50": rand["sum"]["p50"],
            "rand_sum_p95": rand["sum"]["p95"],
            "rand_sh_p50": rand["sharpe_like"]["p50"],
            "rand_sh_p95": rand["sharpe_like"]["p95"],
            "edge_avg_p95": out["avg"] - rand["avg"]["p95"],
            "edge_sum_p95": out["sum"] - rand["sum"]["p95"],
        })
    return out


def main() -> int:
    pools, meta = v2.build_pools()
    universe, selected = pools[POOL]
    bars, bars_meta = v2.base.load_bars_by_symbol(universe + selected, max_minutes=70)
    sel_exec, sel_miss = v2.simulate_combo2(selected, bars, ENTRY, EXIT)
    univ_exec, univ_miss = v2.simulate_combo2(universe, bars, ENTRY, EXIT)
    overall_rand = random_same_ts_exec(univ_exec, sel_exec, 2026061201, v2.SIMS)
    overall = summarize(sel_exec, len(selected), overall_rand)

    # Group signals by BJT signal day, executions by BJT entry day. For practical daily PnL, use entry day.
    selected_by_day: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in selected:
        selected_by_day[bjt_day_of(r)].append(r)
    sel_exec_by_day: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    univ_exec_by_day: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in sel_exec:
        sel_exec_by_day[bjt_day_of(r)].append(r)
    for r in univ_exec:
        univ_exec_by_day[bjt_day_of(r)].append(r)

    daily: dict[str, Any] = {}
    for i, day in enumerate(sorted(set(selected_by_day) | set(sel_exec_by_day))):
        rows = sel_exec_by_day.get(day, [])
        univ_rows = univ_exec_by_day.get(day, [])
        rand = random_same_ts_exec(univ_rows, rows, 2026061300 + i * 17, v2.SIMS) if rows else {}
        daily[day] = summarize(rows, len(selected_by_day.get(day, [])), rand if rand else None)

    # Hour-of-day diagnostic, not a production filter.
    hour_rows: defaultdict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for r in sel_exec:
        dt = r["entry_dt"]
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        hour_rows[dt.astimezone(BJ).hour].append(r)
    hourly = {str(h): summarize(rows, len(rows), None) for h, rows in sorted(hour_rows.items())}

    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "C_oldcore_cd60 daily validation. Pool: old_core top20% in new-radar overlap, same-symbol cooldown 60m, cap8/ts, slim12/ts same-ts execution random, all-taker 8bp, BJT entry-day PnL.",
        "pool": POOL,
        "entry": ENTRY.name,
        "exit": EXIT.name,
        "settings": {"sims": v2.SIMS, "cap_per_ts": v2.CAP_PER_TS, "slim_per_ts": v2.SLIM_PER_TS},
        "meta": {"pools": meta, "bars": bars_meta, "selected_misses": dict(sel_miss), "universe_misses": dict(univ_miss)},
        "overall": overall,
        "daily": daily,
        "hourly": hourly,
    }

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = OUT / f"c-oldcore-cd60-daily-validation-{ts}.json"
    md_path = OUT / f"c-oldcore-cd60-daily-validation-{ts}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")

    lines: list[str] = []
    lines += ["# C_oldcore_cd60 daily validation", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], ""]
    counts = meta["counts"][POOL]
    lines += ["## Coverage", "```text", f"new_window_utc={meta['new_window_utc']}", f"selected_raw={counts['selected_raw']} selected_cap={counts['selected_cap']} slim_universe={counts['slim_universe']} symbols={counts['symbols']} timestamps={counts['timestamps']}", f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}", f"selected_misses={dict(sel_miss)}", "```", ""]
    o = overall
    lines += ["## Overall", "```text", f"n={o['n']}/{o['selected_n']} exec={o['exec_rate']*100:.1f}% symbols={o['symbols']} avg={pct(o['avg'])} rand95={pct(o['rand_avg_p95'])} edge={pct(o['edge_avg_p95'])} sum={pct(o['sum'])}/{pct(o['rand_sum_p95'])} sh={o['sharpe_like']:.2f}/{o['rand_sh_p95']:.2f} mdd={pct(o['mdd'])} cap5={pct(o['cap5'])} cap10={pct(o['cap10'])} remT5={pct(o['remove_top5_avg'])} win={o['win_rate']*100:.1f}% stop={o['initial_stop_rate']*100:.1f}% tp2={o['tp2_rate']*100:.1f}% lock={o['lock_rate']*100:.1f}% time={o['time_rate']*100:.1f}% avg_mfe={pct(o['avg_mfe'])} avg_mae={pct(o['avg_mae'])}", f"reasons={o['reason_counts']}", "```", ""]
    lines += ["## Daily BJT", "```text"]
    for day, d in daily.items():
        edge = d.get("edge_avg_p95", 0.0)
        lines.append(f"{day} n={d['n']:3d}/{d['selected_n']:<3d} exec={d['exec_rate']*100:5.1f}% sym={d['symbols']:2d} avg={pct(d['avg']):>8} rand95={pct(d.get('rand_avg_p95',0)):>8} edge={pct(edge):>8} sum={pct(d['sum']):>8}/{pct(d.get('rand_sum_p95',0)):>8} sh={d['sharpe_like']:5.2f}/{d.get('rand_sh_p95',0):5.2f} mdd={pct(d['mdd']):>8} cap5={pct(d['cap5']):>8} cap10={pct(d['cap10']):>8} remT5={pct(d['remove_top5_avg']):>8} win={d['win_rate']*100:5.1f}% stop={d['initial_stop_rate']*100:5.1f}% tp2={d['tp2_rate']*100:5.1f}% lock={d['lock_rate']*100:5.1f}% time={d['time_rate']*100:5.1f}% mfe={pct(d['avg_mfe']):>8} mae={pct(d['avg_mae']):>8}")
    lines += ["```", ""]
    lines += ["## Daily symbol/reason detail", ""]
    for day, d in daily.items():
        lines += [f"### {day}", "```text", f"reasons={d['reason_counts']}", f"top_by_count={d['top_symbols_by_count']}", f"top_by_pnl={d['top_symbols_by_pnl']}", f"bottom_by_pnl={d['bottom_symbols_by_pnl']}", "```", ""]
    lines += ["## Hour diagnostic BJT (not a hard filter)", "```text"]
    for h, d in hourly.items():
        if d['n'] < 3:
            continue
        lines.append(f"h{int(h):02d} n={d['n']:3d} avg={pct(d['avg']):>8} sum={pct(d['sum']):>8} sh={d['sharpe_like']:5.2f} mdd={pct(d['mdd']):>8} win={d['win_rate']*100:5.1f}% stop={d['initial_stop_rate']*100:5.1f}% remT5={pct(d['remove_top5_avg']):>8}")
    lines += ["```", ""]
    md_path.write_text("\n".join(lines) + "\n")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
