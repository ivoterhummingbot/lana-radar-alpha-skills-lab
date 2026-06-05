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
CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NO_21 = CORE_HOURS - {21}
NO_23 = CORE_HOURS - {23}
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))
DAWN_HOURS = set(range(4, 8))
COST = float(COSTS.get("all_taker", 0.0008))
SEARCH_SIMS = 80
VALIDATE_SIMS = 600
EXEC_START_UTC = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    pullback: float = 0.0
    watch_minutes: int = 0


@dataclass(frozen=True)
class Exit:
    name: str
    kind: str
    tp1: float | None = None
    tp2: float | None = None
    tp1_size: float = 0.5
    lock: float = 0.0
    sl: float = -0.010
    time_minutes: int = 20
    activation: float | None = None
    trail: float | None = None


ENTRIES = [
    Entry("N5", "next5m"),
    Entry("PB03_w10", "pullback", 0.003, 10),
    Entry("PB05_w15", "pullback", 0.005, 15),
    Entry("PB07_w20", "pullback", 0.007, 20),
    Entry("PB10_w25", "pullback", 0.010, 25),
]

EXITS = [
    Exit("P04_08_BE15", "static", tp1=0.004, tp2=0.008, lock=0.0, sl=-0.008, time_minutes=15),
    Exit("P06_12_BE20", "static", tp1=0.006, tp2=0.012, lock=0.0, sl=-0.010, time_minutes=20),
    Exit("P08_16_BE30", "static", tp1=0.008, tp2=0.016, lock=0.0, sl=-0.012, time_minutes=30),
    Exit("P10_20_L02_45", "static", tp1=0.010, tp2=0.020, lock=0.002, sl=-0.015, time_minutes=45),
    Exit("F06_SL10_15", "static", tp1=0.006, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.010, time_minutes=15),
    Exit("TR04_T03_20", "trail", activation=0.004, trail=0.003, sl=-0.008, time_minutes=20, lock=0.0),
    Exit("TR06_T04_30", "trail", activation=0.006, trail=0.004, sl=-0.010, time_minutes=30, lock=0.0),
    Exit("TR08_T05_45", "trail", activation=0.008, trail=0.005, sl=-0.012, time_minutes=45, lock=0.001),
]


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


def top_fraction(rows: Sequence[Mapping[str, Any]], key: str, frac: float) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        n = max(1, math.ceil(len(g) * frac))
        out.extend(sorted(g, key=lambda r: (-_num(r.get(key)), str(r.get("symbol"))))[:n])
    return out


def cap_by_ts(rows: Sequence[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        out.extend(sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("entry_trigger_score")), -_num(r.get("momentum_confirmation_score")), str(r.get("symbol"))))[:cap])
    return out


def cooldown_symbol(rows: Sequence[Mapping[str, Any]], minutes: int) -> list[dict[str, Any]]:
    last: dict[str, datetime] = {}
    out: list[dict[str, Any]] = []
    for r in sorted([dict(x) for x in rows], key=lambda x: (x["ts_dt"], -_num(x.get("market_confirmation_score")), str(x.get("symbol")))):
        s = str(r.get("symbol"))
        if s in last and (r["ts_dt"] - last[s]).total_seconds() < minutes * 60:
            continue
        last[s] = r["ts_dt"]
        out.append(r)
    return out


def attach_raw(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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


def build_old_pools() -> tuple[dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]], dict[str, Any]]:
    rows, meta = load_old_radar_rows()
    rows = [dict(r) for r in rows if r.get("return_1h") is not None and r["ts_dt"] >= EXEC_START_UTC]
    core = [r for r in rows if int(r.get("hour_bjt") or 0) in CORE_HOURS]
    no21 = [r for r in rows if int(r.get("hour_bjt") or 0) in NO_21]
    no23 = [r for r in rows if int(r.get("hour_bjt") or 0) in NO_23]
    night = [r for r in rows if int(r.get("hour_bjt") or 0) in NIGHT_HOURS]
    dawn = [r for r in rows if int(r.get("hour_bjt") or 0) in DAWN_HOURS]

    raw: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    # Canonical old-core selector variants.
    for base_name, universe in [("core", core), ("no21", no21), ("no23", no23), ("night", night), ("dawn", dawn)]:
        if len(universe) < 100:
            continue
        m20 = top_fraction(universe, "market_confirmation_score", 0.20)
        m30 = top_fraction(universe, "market_confirmation_score", 0.30)
        momo40 = top_fraction(universe, "momentum_confirmation_score", 0.40)
        momo40_mhi = top_fraction(momo40, "market_confirmation_score", 0.50)
        raw[f"{base_name}_mkt20"] = (universe, m20)
        raw[f"{base_name}_mkt20_cd60"] = (universe, cooldown_symbol(m20, 60))
        raw[f"{base_name}_mkt30_cd60"] = (universe, cooldown_symbol(m30, 60))
        raw[f"{base_name}_momo40_mkt50_cd60"] = (momo40, cooldown_symbol(momo40_mhi, 60))

    pools: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    counts: dict[str, Any] = {}
    all_rows_for_raw: list[dict[str, Any]] = []
    for k, (univ, sel) in raw.items():
        sel_cap = cap_by_ts(sel, 8)
        univ_cap = cap_by_ts(univ, 20)
        pools[k] = (univ_cap, sel_cap)
        all_rows_for_raw.extend(univ_cap)
        all_rows_for_raw.extend(sel_cap)
        counts[k] = {"universe": len(univ_cap), "selected": len(sel_cap), "raw_selected": len(sel), "symbols": len({str(r.get("symbol")) for r in sel_cap}), "timestamps": len({r["ts_dt"] for r in sel_cap})}

    raw_attached, raw_meta = attach_raw(all_rows_for_raw)
    by_key = {(r["ts_dt"], str(r["symbol"])): r for r in raw_attached}
    attached_pools: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for k, (univ, sel) in pools.items():
        au = [by_key[(r["ts_dt"], str(r["symbol"]))] for r in univ if (r["ts_dt"], str(r["symbol"])) in by_key]
        ase = [by_key[(r["ts_dt"], str(r["symbol"]))] for r in sel if (r["ts_dt"], str(r["symbol"])) in by_key]
        attached_pools[k] = (au, ase)
    meta2 = {"old": meta, "exec_start_utc": iso(EXEC_START_UTC), "counts": counts, "raw_symbol": raw_meta, "search_sims": SEARCH_SIMS, "validate_sims": VALIDATE_SIMS, "cost": "all-taker 8bp"}
    return attached_pools, meta2


def decide_entry(bars: Sequence[Sequence[Any]], signal_dt: datetime, spec: Entry) -> tuple[datetime | None, float | None, str]:
    if spec.kind == "next5m":
        return v2.decide_entry2(bars, signal_dt, v2.Entry2("N5", "next5m"))
    return v2.decide_entry2(bars, signal_dt, v2.Entry2(spec.name, "pullback", pullback=spec.pullback, watch_minutes=spec.watch_minutes))


def bars_from(bars: Sequence[Sequence[Any]], start: datetime, end: datetime) -> list[Sequence[Any]]:
    return v2.base._bars_from(bars, start, end)


def trail_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, xs: Exit) -> dict[str, Any]:
    end = entry_dt + timedelta(minutes=xs.time_minutes)
    path = bars_from(bars, entry_dt, end)
    if entry_px <= 0 or not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    sl_px = entry_px * (1 + xs.sl)
    act = float(xs.activation or 0.0)
    trail = float(xs.trail or 0.0)
    act_px = entry_px * (1 + act)
    left = 1.0
    pnl = 0.0
    active = False
    high_water = entry_px
    trail_px = entry_px * (1 + xs.lock)
    for b in path:
        high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
        if not active:
            if low <= sl_px:
                return {"pnl": xs.sl - COST, "gross_pnl": xs.sl, "reason": "initial_stop", "mfe": mfe, "mae": mae}
            if high >= act_px:
                pnl += xs.tp1_size * act
                left -= xs.tp1_size
                active = True
                high_water = high
                trail_px = max(entry_px * (1 + xs.lock), high_water * (1 - trail))
                continue
        else:
            if low <= trail_px:
                pnl += left * (trail_px / entry_px - 1.0)
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_stop", "mfe": mfe, "mae": mae}
            high_water = max(high_water, high)
            trail_px = max(trail_px, high_water * (1 - trail), entry_px * (1 + xs.lock))
        if close_ms >= int(end.timestamp() * 1000):
            pnl += left * (close / entry_px - 1.0)
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}
    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1.0)
    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}


def simulate(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], es: Entry, xs: Exit) -> tuple[list[dict[str, Any]], Counter[str]]:
    out: list[dict[str, Any]] = []
    miss: Counter[str] = Counter()
    static_xs = None
    if xs.kind == "static":
        static_xs = v2.base.ExitSpec(xs.name, tp1=float(xs.tp1 or 0), tp2=xs.tp2, tp1_size=xs.tp1_size, lock=xs.lock, sl=xs.sl, time_minutes=xs.time_minutes, full_tp=xs.tp2 is None)
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        entry_dt, entry_px, er = decide_entry(bars, r["ts_dt"], es)
        r["entry_rule"] = es.name
        r["exit_rule"] = xs.name
        if entry_dt is None or entry_px is None:
            miss[er] += 1
            continue
        sim = v2.base.simulate_exit(bars, entry_dt, entry_px, static_xs) if static_xs is not None else trail_exit(bars, entry_dt, entry_px, xs)
        if sim.get("pnl") is None:
            miss[str(sim.get("reason"))] += 1
            continue
        r.update(sim)
        r["entry_dt"] = iso(entry_dt)
        r["entry_price"] = entry_px
        out.append(r)
    return out, miss


def q(xs: Sequence[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))]


def random_same_ts(univ_exec: Sequence[Mapping[str, Any]], sel_exec: Sequence[Mapping[str, Any]], seed: int, sims: int) -> dict[str, Any]:
    u_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    s_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in univ_exec:
        u_by[r["ts_dt"]].append(r)
    for r in sel_exec:
        s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    shs: list[float] = []
    for _ in range(sims):
        vals: list[float] = []
        for ts, sg in s_by.items():
            pool = sorted(u_by.get(ts, []), key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("momentum_confirmation_score")), str(r.get("symbol"))))
            if not pool:
                continue
            n = len(sg)
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            vals.extend(float(r["pnl"]) for r in sample)
        st = stat(vals)
        avgs.append(st["avg"]); sums.append(st["sum"]); shs.append(st["sharpe_like"])
    return {"avg_p50": q(avgs, .5), "avg_p95": q(avgs, .95), "sum_p50": q(sums, .5), "sum_p95": q(sums, .95), "sh_p50": q(shs, .5), "sh_p95": q(shs, .95), "sims": sims}


def cap_portfolio(rows: Sequence[Mapping[str, Any]], cap: int) -> dict[str, float]:
    by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(r)
    vals: list[float] = []
    for _ts, g in sorted(by.items()):
        vals.extend(float(r["pnl"]) for r in sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("entry_trigger_score")), str(r.get("symbol"))))[:cap])
    comp = 1.0
    for v in vals:
        comp *= 1.0 + v
    st = stat(vals)
    return {"n": len(vals), "avg": st["avg"], "sum": st["sum"], "sh": st["sharpe_like"], "comp": comp - 1.0, "mdd": v2.base.max_drawdown(vals)}


def summarize(pool: str, selected_n: int, rows: Sequence[Mapping[str, Any]], rand: Mapping[str, Any], miss: Counter[str]) -> dict[str, Any]:
    vals = [float(r["pnl"]) for r in rows]
    st = stat(vals)
    reasons = Counter(str(r.get("reason")) for r in rows)
    days: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        days[bjt_day(r)].append(float(r["pnl"]))
    day_sums = {d: sum(v) for d, v in sorted(days.items())}
    pnl_by_sym: defaultdict[str, float] = defaultdict(float)
    n_by_sym: Counter[str] = Counter()
    for r in rows:
        s = str(r.get("symbol"))
        pnl_by_sym[s] += float(r["pnl"])
        n_by_sym[s] += 1
    top = sorted(pnl_by_sym.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_syms = {s for s, _ in top}
    rem = [float(r["pnl"]) for r in rows if str(r.get("symbol")) not in top_syms]
    cap5 = cap_portfolio(rows, 5)
    cap10 = cap_portfolio(rows, 10)
    out = {
        "pool": pool,
        "selected_n": selected_n,
        "n": len(rows),
        "exec_rate": len(rows) / selected_n if selected_n else 0.0,
        "symbols": len({str(r.get("symbol")) for r in rows}),
        "avg": st["avg"], "sum": st["sum"], "sh": st["sharpe_like"], "mdd": v2.base.max_drawdown(vals),
        "win": sum(1 for v in vals if v > 0) / len(vals) if vals else 0.0,
        "rand_avg_p95": rand.get("avg_p95", 0.0), "rand_sum_p95": rand.get("sum_p95", 0.0), "rand_sh_p95": rand.get("sh_p95", 0.0),
        "edge_avg_p95": st["avg"] - rand.get("avg_p95", 0.0), "edge_sum_p95": st["sum"] - rand.get("sum_p95", 0.0),
        "cap5": cap5, "cap10": cap10,
        "remove_top5_avg": stat(rem)["avg"] if rem else 0.0,
        "positive_days": sum(1 for v in day_sums.values() if v > 0), "days": len(day_sums), "day_sums": day_sums,
        "initial_stop_rate": reasons.get("initial_stop", 0) / len(rows) if rows else 0.0,
        "reason_counts": dict(reasons), "misses": dict(miss),
        "top_symbols": [{"symbol": s, "pnl": pnl, "n": n_by_sym[s]} for s, pnl in top],
        "avg_mfe": sum(float(r.get("mfe", 0.0) or 0.0) for r in rows) / len(rows) if rows else 0.0,
        "avg_mae": sum(float(r.get("mae", 0.0) or 0.0) for r in rows) / len(rows) if rows else 0.0,
    }
    passes = {
        "avg_gt_rand_p95": out["avg"] > out["rand_avg_p95"],
        "sum_gt_rand_p95": out["sum"] > out["rand_sum_p95"],
        "sh_gt_rand_p95": out["sh"] > out["rand_sh_p95"],
        "cap5_pos": cap5["comp"] > 0,
        "cap10_pos": cap10["comp"] > 0,
        "remove_top5_avg_pos": out["remove_top5_avg"] >= 0,
        "days_all_pos": out["positive_days"] == out["days"] and out["days"] >= 3,
        "stop_le_25pct": out["initial_stop_rate"] <= 0.25,
        "n_ge_80": out["n"] >= 80,
    }
    out["passes"] = passes
    out["pass_n"] = sum(1 for v in passes.values() if v)
    return out


def render(result: Mapping[str, Any]) -> str:
    lines = ["# Old radar execution alpha second-stage search", "", f"generated_utc: `{result['generated_utc']}`", "", "## Meta", "```text", json.dumps(result["meta"], ensure_ascii=False, indent=2, default=str), "```", "", "## Leaderboard", "```text"]
    for r in result["leaderboard"][:30]:
        lines.append(f"{r['rank']:02d} {r['pool']:<26} {r['entry']+'__'+r['exit']:<26} pass={r['pass_n']}/9 n={r['n']:4d}/{r['selected_n']:<4d} ex={r['exec_rate']*100:5.1f}% avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']['comp']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['positive_days']}/{r['days']} stop={r['initial_stop_rate']*100:4.1f}% mfe={pct(r['avg_mfe']):>8}")
    lines += ["```", "", "## Validated top candidates", "```text"]
    for r in result["validated"]:
        lines.append(f"{r['pool']:<26} {r['entry']+'__'+r['exit']:<26} pass={r['pass_n']}/9 n={r['n']:4d}/{r['selected_n']:<4d} ex={r['exec_rate']*100:5.1f}% avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']['comp']):>8} cap10={pct(r['cap10']['comp']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['positive_days']}/{r['days']} stop={r['initial_stop_rate']*100:4.1f}% win={r['win']*100:4.1f}% mfe={pct(r['avg_mfe']):>8} mae={pct(r['avg_mae']):>8}")
        lines.append(f"  reasons={r['reason_counts']} misses={r['misses']}")
        lines.append("  top=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in r['top_symbols']))
        lines.append("  days=" + ", ".join(f"{d}:{pct(v)}" for d, v in r['day_sums'].items()))
    lines += ["```", "", "## Working conclusion", "```text", result["conclusion"], "```", ""]
    return "\n".join(lines)


def main() -> int:
    pools, meta = build_old_pools()
    all_rows: list[dict[str, Any]] = []
    for u, s in pools.values():
        all_rows.extend(u); all_rows.extend(s)
    bars, bars_meta = v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in EXITS) + 45)
    meta["bars"] = bars_meta
    leaderboard: list[dict[str, Any]] = []
    idx = 0
    exec_cache: dict[tuple[str, str, str], tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]] = {}
    for pool_name, (univ, selected) in pools.items():
        if len(selected) < 40:
            continue
        for es in ENTRIES:
            for xs in EXITS:
                idx += 1
                sel_exec, miss = simulate(selected, bars, es, xs)
                if len(sel_exec) < 30:
                    continue
                univ_exec, _ = simulate(univ, bars, es, xs)
                if len(univ_exec) <= len(sel_exec):
                    continue
                rand = random_same_ts(univ_exec, sel_exec, 2026061400 + idx * 13, SEARCH_SIMS)
                s = summarize(pool_name, len(selected), sel_exec, rand, miss)
                s.update({"entry": es.name, "exit": xs.name})
                leaderboard.append(s)
                exec_cache[(pool_name, es.name, xs.name)] = (sel_exec, univ_exec, miss)
    leaderboard.sort(key=lambda r: (r["pass_n"], r["edge_avg_p95"], r["sh"], r["sum"]), reverse=True)
    for rank, r in enumerate(leaderboard, 1):
        r["rank"] = rank
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in leaderboard[:12]:
        key = (r["pool"], r["entry"], r["exit"])
        if key in seen:
            continue
        seen.add(key)
        sel_exec, univ_exec, miss = exec_cache[key]
        rand = random_same_ts(univ_exec, sel_exec, 2026061500 + len(seen) * 101, VALIDATE_SIMS)
        v = summarize(r["pool"], r["selected_n"], sel_exec, rand, miss)
        v.update({"entry": r["entry"], "exit": r["exit"], "rank_from_search": r["rank"]})
        validated.append(v)
    conclusion = "Search result is execution-layer alpha, not new discovery: old-core/no21 market-score pools with next5m or shallow pullback plus tight partial-profit exits are currently strongest; require daily fresh-forward random95 monitoring before production."
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "meta": meta, "leaderboard": leaderboard, "validated": validated, "conclusion": conclusion}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"old-execution-alpha-second-stage-{ts}.json"
    mp = OUT / f"old-execution-alpha-second-stage-{ts}.md"
    latest_jp = OUT / "old-execution-alpha-second-stage-latest.json"
    latest_mp = OUT / "old-execution-alpha-second-stage-latest.md"
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
