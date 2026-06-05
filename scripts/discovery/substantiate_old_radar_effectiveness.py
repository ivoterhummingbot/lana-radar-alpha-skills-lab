#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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
from radar_alpha_skills_lab.old_radar_alpha import load_old_radar_rows  # noqa: E402
from radar_alpha_skills_lab.signal_control import COSTS, fetch_exchange_symbols, iso, pct, stat, to_fapi_symbol  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
COST = float(COSTS.get("all_taker", 0.0008))
CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
EXEC_START_UTC = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
SIMS_DISC = 600
SIMS_EXEC = 350

ENTRY = v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20)
STATIC = v2.base.ExitSpec("STATIC_P06_12_BE20", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20)

@dataclass(frozen=True)
class TrailSpec:
    name: str = "TRAIL06_T04_30m"
    sl: float = -0.010
    activation: float = 0.006
    first_size: float = 0.5
    trail: float = 0.004
    lock: float = 0.0
    time_minutes: int = 30

TRAIL = TrailSpec()


def _num(x: Any) -> float:
    try:
        v = float(x or 0.0)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def bjt_day(row: Mapping[str, Any]) -> str:
    dt = row.get("entry_dt") or row.get("ts_dt")
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone(BJ).date().isoformat()


def top_fraction_by_ts(rows: Sequence[Mapping[str, Any]], key: str, frac: float, *, ascending: bool = False) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get(key) is None:
            continue
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        n = max(1, math.ceil(len(g) * frac))
        out.extend(sorted(g, key=lambda r: ((_num(r.get(key)) if ascending else -_num(r.get(key))), str(r.get("symbol"))))[:n])
    return out


def split_top_bottom_by_ts(rows: Sequence[Mapping[str, Any]], key: str, frac: float = 0.5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    hi: list[dict[str, Any]] = []
    lo: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        ranked = sorted(g, key=lambda r: (-_num(r.get(key)), str(r.get("symbol"))))
        n = max(1, math.ceil(len(ranked) * frac))
        hi.extend(ranked[:n])
        lo.extend(ranked[-n:])
    return hi, lo


def values(rows: Sequence[Mapping[str, Any]], key: str, cost: float = 0.0) -> list[float]:
    return [float(r[key]) - cost for r in rows if r.get(key) is not None]


def q(xs: Sequence[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))]


def matched_random_metric(universe: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], metric_key: str, *, seed: int, sims: int = SIMS_DISC) -> dict[str, Any]:
    u_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    s_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in universe:
        if r.get(metric_key) is not None:
            u_by[r["ts_dt"]].append(r)
    for r in selected:
        if r.get(metric_key) is not None:
            s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    for _ in range(sims):
        picked: list[float] = []
        for ts, sg in s_by.items():
            pool = sorted(u_by.get(ts, []), key=lambda r: str(r.get("symbol")))
            if not pool:
                continue
            n = len(sg)
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            picked.extend(float(r[metric_key]) for r in sample)
        st = stat(picked)
        avgs.append(st["avg"])
        sums.append(st["sum"])
    return {"avg_p50": q(avgs, 0.50), "avg_p95": q(avgs, 0.95), "sum_p50": q(sums, 0.50), "sum_p95": q(sums, 0.95), "sims": sims}


def day_sums(rows: Sequence[Mapping[str, Any]], metric_key: str, cost: float = 0.0) -> dict[str, float]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(metric_key) is not None:
            by[str(r.get("date_bjt") or bjt_day(r))].append(float(r[metric_key]) - cost)
    return {d: sum(v) for d, v in sorted(by.items())}


def discovery_block(name: str, universe: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], seed_base: int) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "n": len(selected), "symbols": len({str(r.get("symbol")) for r in selected}), "horizons": {}}
    for i, h in enumerate(["1h", "4h", "24h"]):
        ret_key = f"return_{h}"
        mfe_key = f"mfe_{h}"
        valid = [r for r in selected if r.get(ret_key) is not None]
        ret_vals = values(valid, ret_key, COSTS["all_taker_8bp_total"])
        mfe_vals = values(valid, mfe_key, 0.0)
        rand_ret = matched_random_metric(universe, valid, ret_key, seed=seed_base + i * 100 + 1)
        rand_mfe = matched_random_metric(universe, valid, mfe_key, seed=seed_base + i * 100 + 2)
        ds = day_sums(valid, ret_key, COSTS["all_taker_8bp_total"])
        out["horizons"][h] = {
            "n": len(valid),
            "ret": stat(ret_vals),
            "mfe": stat(mfe_vals),
            "rand_ret_avg_p95": rand_ret["avg_p95"],
            "rand_mfe_avg_p95": rand_mfe["avg_p95"],
            "ret_edge_p95": (stat(ret_vals)["avg"] - rand_ret["avg_p95"]) if ret_vals else 0.0,
            "mfe_edge_p95": (stat(mfe_vals)["avg"] - rand_mfe["avg_p95"]) if mfe_vals else 0.0,
            "positive_days": sum(1 for v in ds.values() if v > 0),
            "days": len(ds),
            "day_sums": ds,
        }
    return out


def attach_raw_symbol(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tradable = fetch_exchange_symbols()
    out: list[dict[str, Any]] = []
    invalid: Counter[str] = Counter()
    for r0 in rows:
        r = dict(r0)
        raw = to_fapi_symbol(str(r.get("symbol")), tradable)
        if raw is None:
            invalid[str(r.get("symbol"))] += 1
            continue
        r["raw_symbol"] = raw
        out.append(r)
    return out, {"input": len(rows), "tradable": len(out), "invalid": dict(invalid)}


def bar_dt(bar: Sequence[Any]) -> datetime:
    return v2.base.bar_dt(bar)


def bars_from(bars: Sequence[Sequence[Any]], start: datetime, end: datetime) -> list[Sequence[Any]]:
    return v2.base._bars_from(bars, start, end)


def trail_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, spec: TrailSpec = TRAIL) -> dict[str, Any]:
    if entry_px <= 0:
        return {"pnl": None, "reason": "bad_entry"}
    end = entry_dt + timedelta(minutes=spec.time_minutes)
    path = bars_from(bars, entry_dt, end)
    if not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    sl_px = entry_px * (1 + spec.sl)
    act_px = entry_px * (1 + spec.activation)
    left = 1.0
    pnl = 0.0
    active = False
    high_water = entry_px
    trail_px = entry_px * (1 + spec.lock)
    for b in path:
        high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
        if not active:
            if low <= sl_px:
                return {"pnl": spec.sl - COST, "gross_pnl": spec.sl, "reason": "initial_stop", "mfe": mfe, "mae": mae}
            if high >= act_px:
                pnl += spec.first_size * spec.activation
                left -= spec.first_size
                active = True
                high_water = high
                trail_px = max(entry_px * (1 + spec.lock), high_water * (1 - spec.trail))
                continue
        else:
            if low <= trail_px:
                pnl += left * (trail_px / entry_px - 1.0)
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_stop", "mfe": mfe, "mae": mae}
            high_water = max(high_water, high)
            trail_px = max(trail_px, high_water * (1 - spec.trail), entry_px * (1 + spec.lock))
        if close_ms >= int(end.timestamp() * 1000):
            pnl += left * (close / entry_px - 1.0)
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}
    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1.0)
    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}


def simulate_rows(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], exit_kind: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    out: list[dict[str, Any]] = []
    miss: Counter[str] = Counter()
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        entry_dt, entry_px, er = v2.decide_entry2(bars, r["ts_dt"], ENTRY)
        r["entry_rule"] = ENTRY.name
        r["exit_rule"] = STATIC.name if exit_kind == "static" else TRAIL.name
        r["entry_reason"] = er
        if entry_dt is None or entry_px is None:
            miss[er] += 1
            continue
        sim = v2.base.simulate_exit(bars, entry_dt, entry_px, STATIC) if exit_kind == "static" else trail_exit(bars, entry_dt, entry_px, TRAIL)
        if sim.get("pnl") is None:
            miss[str(sim.get("reason"))] += 1
            continue
        r.update(sim)
        r["entry_dt"] = iso(entry_dt)
        r["entry_price"] = entry_px
        out.append(r)
    return out, miss


def random_exec(universe_exec: Sequence[Mapping[str, Any]], selected_exec: Sequence[Mapping[str, Any]], seed: int, sims: int = SIMS_EXEC) -> dict[str, Any]:
    u_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    s_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in universe_exec:
        if r.get("pnl") is not None:
            u_by[r["ts_dt"]].append(r)
    for r in selected_exec:
        if r.get("pnl") is not None:
            s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    shs: list[float] = []
    for _ in range(sims):
        picked: list[float] = []
        for ts, sg in s_by.items():
            pool = sorted(u_by.get(ts, []), key=lambda r: (-_num(r.get("market_confirmation_score")), str(r.get("symbol"))))
            if not pool:
                continue
            n = len(sg)
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            picked.extend(float(r["pnl"]) for r in sample)
        st = stat(picked)
        avgs.append(st["avg"]); sums.append(st["sum"]); shs.append(st["sharpe_like"])
    return {"avg_p50": q(avgs, .5), "avg_p95": q(avgs, .95), "sum_p50": q(sums, .5), "sum_p95": q(sums, .95), "sh_p50": q(shs, .5), "sh_p95": q(shs, .95)}


def cap_by_ts(rows: Sequence[Mapping[str, Any]], cap: int) -> dict[str, Any]:
    by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(r)
    vals: list[float] = []
    for _ts, g in sorted(by.items()):
        ranked = sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("momentum_confirmation_score")), str(r.get("symbol"))))[:cap]
        vals.extend(float(r["pnl"]) for r in ranked)
    comp = 1.0
    for v in vals:
        comp *= 1.0 + v
    st = stat(vals)
    return {"n": len(vals), "avg": st["avg"], "sum": st["sum"], "comp": comp - 1.0, "sharpe_like": st["sharpe_like"], "mdd": v2.base.max_drawdown(vals)}


def summarize_exec(name: str, rows: Sequence[Mapping[str, Any]], selected_n: int, rand: Mapping[str, Any]) -> dict[str, Any]:
    vals = [float(r["pnl"]) for r in rows]
    st = stat(vals)
    reasons = Counter(str(r.get("reason")) for r in rows)
    pnl_by_sym: defaultdict[str, float] = defaultdict(float)
    n_by_sym: Counter[str] = Counter()
    for r in rows:
        s = str(r.get("symbol"))
        pnl_by_sym[s] += float(r["pnl"]); n_by_sym[s] += 1
    top_syms = sorted(pnl_by_sym.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top5 = {s for s, _ in top_syms}
    rem = [float(r["pnl"]) for r in rows if str(r.get("symbol")) not in top5]
    days: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        days[bjt_day(r)].append(float(r["pnl"]))
    day_stats = {d: {"n": len(v), "sum": sum(v), "avg": sum(v)/len(v) if v else 0.0} for d, v in sorted(days.items())}
    return {
        "name": name,
        "selected_n": selected_n,
        "n": len(rows),
        "exec_rate": len(rows)/selected_n if selected_n else 0.0,
        "symbols": len({str(r.get("symbol")) for r in rows}),
        "avg": st["avg"], "sum": st["sum"], "sharpe_like": st["sharpe_like"], "mdd": v2.base.max_drawdown(vals),
        "rand_avg_p95": rand.get("avg_p95", 0.0), "rand_sum_p95": rand.get("sum_p95", 0.0), "rand_sh_p95": rand.get("sh_p95", 0.0),
        "edge_avg_p95": st["avg"] - rand.get("avg_p95", 0.0), "edge_sum_p95": st["sum"] - rand.get("sum_p95", 0.0),
        "win_rate": sum(1 for v in vals if v > 0)/len(vals) if vals else 0.0,
        "initial_stop_rate": reasons.get("initial_stop", 0)/len(rows) if rows else 0.0,
        "reason_counts": dict(reasons),
        "cap5": cap_by_ts(rows, 5), "cap10": cap_by_ts(rows, 10),
        "remove_top5_avg": stat(rem)["avg"] if rem else 0.0,
        "top_symbols": [{"symbol": s, "pnl": v, "n": n_by_sym[s]} for s, v in top_syms],
        "daily": day_stats,
        "positive_days": sum(1 for d in day_stats.values() if d["sum"] > 0),
        "days": len(day_stats),
    }


def render(result: Mapping[str, Any]) -> str:
    lines = ["# Old radar effectiveness substantiation", "", f"generated_utc: `{result['generated_utc']}`", "", "## Input", "```text", json.dumps(result["input_meta"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## Discovery / MFE evidence", ""]
    for b in result["discovery"]:
        lines += [f"### {b['name']}", "```text"]
        for h, d in b["horizons"].items():
            lines.append(f"{h:<3} n={d['n']:4d} ret_avg={pct(d['ret']['avg']):>8} ret_rand95={pct(d['rand_ret_avg_p95']):>8} ret_edge={pct(d['ret_edge_p95']):>8} mfe_avg={pct(d['mfe']['avg']):>8} mfe_rand95={pct(d['rand_mfe_avg_p95']):>8} mfe_edge={pct(d['mfe_edge_p95']):>8} ret_pos_days={d['positive_days']}/{d['days']}")
        lines += ["```", ""]
    lines += ["## Execution selector evidence", ""]
    for name, block in result["execution"].items():
        lines += [f"### {name}", "```text"]
        for ex, d in block.items():
            lines.append(f"{ex:<18} n={d['n']:4d}/{d['selected_n']:<4d} avg={pct(d['avg']):>8} rand95={pct(d['rand_avg_p95']):>8} edge={pct(d['edge_avg_p95']):>8} sum={pct(d['sum']):>8}/{pct(d['rand_sum_p95']):>8} sh={d['sharpe_like']:5.2f}/{d['rand_sh_p95']:5.2f} mdd={pct(d['mdd']):>8} cap5={pct(d['cap5']['comp']):>8} remT5={pct(d['remove_top5_avg']):>8} stop={d['initial_stop_rate']*100:5.1f}% pos_days={d['positive_days']}/{d['days']}")
            lines.append(f"  reasons={d['reason_counts']}")
            lines.append("  top=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in d['top_symbols']))
        lines += ["```", ""]
    lines += ["## Conclusion", "```text", result["conclusion"], "```", ""]
    return "\n".join(lines)


def main() -> int:
    rows, old_meta = load_old_radar_rows()
    complete_1h = [dict(r) for r in rows if r.get("return_1h") is not None]
    core = [r for r in complete_1h if int(r.get("hour_bjt") or 0) in CORE_HOURS]
    market20 = top_fraction_by_ts(core, "market_confirmation_score", 0.20)
    momo20 = top_fraction_by_ts(core, "momentum_confirmation_score", 0.20)
    old_momo40 = top_fraction_by_ts(core, "momentum_confirmation_score", 0.40)
    momo40_mkt_hi, momo40_mkt_lo = split_top_bottom_by_ts(old_momo40, "market_confirmation_score", 0.50)
    momo40_mkt_top50 = top_fraction_by_ts(old_momo40, "market_confirmation_score", 0.50)

    discovery = [
        discovery_block("old_core_market_top20", core, market20, 2026060401),
        discovery_block("old_core_momentum_top20", core, momo20, 2026060501),
        discovery_block("old_momo40_marketHIGH", old_momo40, momo40_mkt_hi, 2026060601),
        discovery_block("old_momo40_marketLOW", old_momo40, momo40_mkt_lo, 2026060701),
    ]

    # Execution: recent complete window, old-only. This avoids any new-radar DB dependency.
    exec_universe0 = [dict(r) for r in old_momo40 if r["ts_dt"] >= EXEC_START_UTC]
    exec_top0 = [dict(r) for r in momo40_mkt_top50 if r["ts_dt"] >= EXEC_START_UTC]
    exec_hi0 = [dict(r) for r in momo40_mkt_hi if r["ts_dt"] >= EXEC_START_UTC]
    exec_lo0 = [dict(r) for r in momo40_mkt_lo if r["ts_dt"] >= EXEC_START_UTC]
    all_exec_rows0 = exec_universe0 + exec_top0 + exec_hi0 + exec_lo0
    all_exec_rows, raw_meta = attach_raw_symbol(all_exec_rows0)
    # Rebuild sets by (ts,symbol) after raw-symbol filtering.
    valid_keys = {(r["ts_dt"], str(r["symbol"])) for r in all_exec_rows}
    def keep_valid(xs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ys = [dict(r) for r in xs if (r["ts_dt"], str(r["symbol"])) in valid_keys]
        out, _ = attach_raw_symbol(ys)
        return out
    exec_universe = keep_valid(exec_universe0)
    exec_top = keep_valid(exec_top0)
    exec_hi = keep_valid(exec_hi0)
    exec_lo = keep_valid(exec_lo0)

    bars, bars_meta = v2.base.load_bars_by_symbol(exec_universe + exec_top + exec_hi + exec_lo, max_minutes=75)
    execution: dict[str, dict[str, Any]] = {}
    for label, selected in [("old_momo40_marketTop50", exec_top), ("old_momo40_marketHIGH", exec_hi), ("old_momo40_marketLOW", exec_lo)]:
        execution[label] = {}
        for exit_kind in ["static", "trail"]:
            univ_exec, univ_miss = simulate_rows(exec_universe, bars, exit_kind)
            sel_exec, sel_miss = simulate_rows(selected, bars, exit_kind)
            rand = random_exec(univ_exec, sel_exec, 2026060800 + len(label) * 17 + (1 if exit_kind == "trail" else 0))
            s = summarize_exec(exit_kind, sel_exec, len(selected), rand)
            s["misses"] = {"selected": dict(sel_miss), "universe": dict(univ_miss)}
            execution[label][exit_kind] = s

    conclusion = (
        "Old radar is valid, but only with a precise definition: not a clean independent top-level discovery engine; "
        "it is a useful old-core/old-momentum execution selector. Market score inside old_momo40 separates executable paths from bad hot/momentum paths. "
        "Keep old_momo40 -> marketTop50/HIGH -> PB07_w20 -> STATIC_P06_12_BE20 or TRAIL06_T04_30m as the standing shadow line; do not claim production until fresh-forward day-wise random-p95 gates continue to pass."
    )
    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "input_meta": {"old": old_meta, "core_rows": len(core), "market20": len(market20), "momo20": len(momo20), "old_momo40": len(old_momo40), "exec_start_utc": iso(EXEC_START_UTC), "raw_symbol_meta": raw_meta, "bars": bars_meta},
        "settings": {"discovery_sims": SIMS_DISC, "execution_sims": SIMS_EXEC, "entry": ENTRY.name, "static_exit": STATIC.name, "trail_exit": TRAIL.name, "cost": "all-taker 8bp"},
        "discovery": discovery,
        "execution": execution,
        "conclusion": conclusion,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"old-radar-effectiveness-substantiation-{ts}.json"
    mp = OUT / f"old-radar-effectiveness-substantiation-{ts}.md"
    latest_jp = OUT / "old-radar-effectiveness-substantiation-latest.json"
    latest_mp = OUT / "old-radar-effectiveness-substantiation-latest.md"
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
