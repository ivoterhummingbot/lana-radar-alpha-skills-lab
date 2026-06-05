#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_execution_alpha_focused_v2 as v2  # noqa: E402
import validate_c_cd60_new_radar_gate as gate  # noqa: E402
import validate_c_oldcore_cd60_daily as daily_base  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.signal_control import COSTS, iso, load_snapshot_rows, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
COST = float(COSTS.get("all_taker", 0.0008))
POOL = "C_oldcore_cd60"
v2.SIMS = 220
v2.CAP_PER_TS = 8
v2.SLIM_PER_TS = 12
ENTRY = v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20)


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


@dataclass(frozen=True)
class DynSpec:
    name: str
    mode: str
    sl: float = -0.010
    time_minutes: int = 20
    tp1: float = 0.006
    tp2: float | None = 0.012
    tp1_size: float = 0.5
    lock: float = 0.0
    full_tp: bool = False
    early_minutes: int = 0
    early_need: float = 0.003
    early_cut: float = -0.002
    trail: float = 0.004
    trail_activation: float = 0.006
    trail_tp2: float | None = None


STATIC_P06 = DynSpec("STATIC_P06_12_BE20", "static", sl=-0.010, time_minutes=20, tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0)
STATIC_F06 = DynSpec("STATIC_F06_SL10_10m", "static", sl=-0.010, time_minutes=10, tp1=0.006, tp2=None, tp1_size=1.0, lock=0.0, full_tp=True)
SPECS = [
    STATIC_P06,
    STATIC_F06,
    DynSpec("AF3_P06_12_BE20", "early_static", sl=-0.010, time_minutes=20, tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, early_minutes=3, early_need=0.003, early_cut=-0.002),
    DynSpec("AF5_P06_12_BE20", "early_static", sl=-0.010, time_minutes=20, tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, early_minutes=5, early_need=0.004, early_cut=-0.002),
    DynSpec("AF3_F06_SL10_10m", "early_static", sl=-0.010, time_minutes=10, tp1=0.006, tp2=None, tp1_size=1.0, full_tp=True, early_minutes=3, early_need=0.003, early_cut=-0.002),
    DynSpec("TRAIL06_T04_30m", "trail", sl=-0.010, time_minutes=30, tp1=0.006, tp1_size=0.5, lock=0.0, trail=0.004, trail_activation=0.006, trail_tp2=None),
    DynSpec("TRAIL06_T06_35m", "trail", sl=-0.010, time_minutes=35, tp1=0.006, tp1_size=0.5, lock=0.0, trail=0.006, trail_activation=0.006, trail_tp2=None),
    DynSpec("TRAIL08_T05_35m", "trail", sl=-0.012, time_minutes=35, tp1=0.008, tp1_size=0.5, lock=0.0, trail=0.005, trail_activation=0.008, trail_tp2=None),
    DynSpec("TRAIL06_T04_TP18_35m", "trail", sl=-0.010, time_minutes=35, tp1=0.006, tp1_size=0.5, lock=0.0, trail=0.004, trail_activation=0.006, trail_tp2=0.018),
    DynSpec("REGSPLIT_qualityP06_elseF06", "regime_split"),
    DynSpec("REGSPLIT_qualityTrail_elseF06", "regime_split_trail"),
    DynSpec("SKIPnegBTC_qualityP06_elseF06", "skip_neg_btc_split"),
    DynSpec("SKIPnegBTC_AF3P06", "skip_neg_btc_af"),
]


def bar_dt(bar: Sequence[Any]) -> datetime:
    return v2.base.bar_dt(bar)


def bars_from(bars: Sequence[Sequence[Any]], start: datetime, end: datetime) -> list[Sequence[Any]]:
    return v2.base._bars_from(bars, start, end)


def static_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, spec: DynSpec) -> dict[str, Any]:
    xs = v2.base.ExitSpec(
        spec.name,
        tp1=spec.tp1,
        tp2=spec.tp2,
        tp1_size=spec.tp1_size,
        lock=spec.lock,
        sl=spec.sl,
        time_minutes=spec.time_minutes,
        full_tp=spec.full_tp,
    )
    return v2.base.simulate_exit(bars, entry_dt, entry_px, xs)


def early_static_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, spec: DynSpec) -> dict[str, Any]:
    if entry_px <= 0:
        return {"pnl": None, "reason": "bad_entry"}
    end = entry_dt + timedelta(minutes=spec.time_minutes)
    path = bars_from(bars, entry_dt, end)
    if not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    early_end = entry_dt + timedelta(minutes=spec.early_minutes)
    early_path = [b for b in path if bar_dt(b) <= early_end]
    if early_path:
        early_high = max(float(b[2]) for b in early_path) / entry_px - 1.0
        early_close = float(early_path[-1][4]) / entry_px - 1.0
        # If the trade cannot make minimal progress and is already underwater, cut before the normal stop.
        if early_high < spec.early_need and early_close <= spec.early_cut:
            pnl = early_close - COST
            return {"pnl": pnl, "gross_pnl": early_close, "reason": "early_adverse", "mfe": mfe, "mae": mae, "capture_ratio": pnl / mfe if mfe > 0 else 0.0}
    return static_exit(bars, entry_dt, entry_px, spec)


def trail_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, spec: DynSpec) -> dict[str, Any]:
    if entry_px <= 0:
        return {"pnl": None, "reason": "bad_entry"}
    end = entry_dt + timedelta(minutes=spec.time_minutes)
    path = bars_from(bars, entry_dt, end)
    if not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    sl_px = entry_px * (1.0 + spec.sl)
    act_px = entry_px * (1.0 + spec.trail_activation)
    hard_tp_px = entry_px * (1.0 + spec.trail_tp2) if spec.trail_tp2 is not None else None
    left = 1.0
    pnl = 0.0
    active = False
    high_water = entry_px
    trail_px = entry_px * (1.0 + spec.lock)
    for b in path:
        high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
        if not active:
            # Conservative before activation: initial stop first.
            if low <= sl_px:
                pnl = spec.sl
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "initial_stop", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
            if high >= act_px:
                pnl += spec.tp1_size * spec.trail_activation
                left -= spec.tp1_size
                active = True
                high_water = high
                trail_px = max(entry_px * (1.0 + spec.lock), high_water * (1.0 - spec.trail))
                # Do not allow a same-minute post-activation trail stop; OHLC order is unknowable and this avoids pathological same-bar churn.
                if hard_tp_px is not None and high >= hard_tp_px:
                    pnl += left * (spec.trail_tp2 or 0.0)
                    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_tp2", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
                if close_ms >= int(end.timestamp() * 1000):
                    pnl += left * (close / entry_px - 1.0)
                    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
                continue
        else:
            # Conservative after activation: previous trail stop before updating with current high.
            if low <= trail_px:
                ret = trail_px / entry_px - 1.0
                pnl += left * ret
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_stop", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
            if hard_tp_px is not None and high >= hard_tp_px:
                pnl += left * (spec.trail_tp2 or 0.0)
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_tp2", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
            high_water = max(high_water, high)
            trail_px = max(trail_px, high_water * (1.0 - spec.trail), entry_px * (1.0 + spec.lock))
        if close_ms >= int(end.timestamp() * 1000):
            pnl += left * (close / entry_px - 1.0)
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1.0)
    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}


def quality_gate(row: Mapping[str, Any]) -> bool:
    return _num(row.get("new_prev30_btc_15m_ret")) >= 0.0 and bool(row.get("new_prev15_mkt_top20"))


def btc_nonneg(row: Mapping[str, Any]) -> bool:
    return _num(row.get("new_prev30_btc_15m_ret")) >= 0.0


def simulate_exit_dispatch(row: Mapping[str, Any], bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, spec: DynSpec) -> dict[str, Any]:
    if spec.mode == "static":
        return static_exit(bars, entry_dt, entry_px, spec)
    if spec.mode == "early_static":
        return early_static_exit(bars, entry_dt, entry_px, spec)
    if spec.mode == "trail":
        return trail_exit(bars, entry_dt, entry_px, spec)
    if spec.mode == "regime_split":
        return static_exit(bars, entry_dt, entry_px, STATIC_P06 if quality_gate(row) else STATIC_F06)
    if spec.mode == "regime_split_trail":
        trail = DynSpec("INNER_TRAIL06_T04_30m", "trail", sl=-0.010, time_minutes=30, tp1=0.006, tp1_size=0.5, lock=0.0, trail=0.004, trail_activation=0.006)
        return trail_exit(bars, entry_dt, entry_px, trail) if quality_gate(row) else static_exit(bars, entry_dt, entry_px, STATIC_F06)
    if spec.mode == "skip_neg_btc_split":
        if not btc_nonneg(row):
            return {"pnl": None, "reason": "skip_neg_btc"}
        return static_exit(bars, entry_dt, entry_px, STATIC_P06 if quality_gate(row) else STATIC_F06)
    if spec.mode == "skip_neg_btc_af":
        if not btc_nonneg(row):
            return {"pnl": None, "reason": "skip_neg_btc"}
        af = DynSpec("INNER_AF3_P06_12_BE20", "early_static", sl=-0.010, time_minutes=20, tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, early_minutes=3, early_need=0.003, early_cut=-0.002)
        return early_static_exit(bars, entry_dt, entry_px, af)
    raise ValueError(spec.mode)


def simulate_rows(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], spec: DynSpec) -> tuple[list[dict[str, Any]], dict[str, int]]:
    out: list[dict[str, Any]] = []
    miss: dict[str, int] = defaultdict(int)
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        entry_dt, entry_px, er = v2.decide_entry2(bars, r["ts_dt"], ENTRY)
        r["entry_rule"] = ENTRY.name
        r["exit_rule"] = spec.name
        r["entry_reason"] = er
        if entry_dt is None or entry_px is None:
            miss[er] += 1
            continue
        sim = simulate_exit_dispatch(r, bars, entry_dt, float(entry_px), spec)
        if sim.get("pnl") is None:
            miss[str(sim.get("reason"))] += 1
            continue
        r.update(sim)
        r["entry_dt"] = iso(entry_dt)
        r["entry_price"] = entry_px
        out.append(r)
    return out, dict(miss)


def random_same_ts(universe_exec: Sequence[Mapping[str, Any]], selected_exec: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    return v2.random_same_ts(universe_exec, selected_exec, seed) if selected_exec and universe_exec else {}


def daily_edges(rows: Sequence[Mapping[str, Any]], universe_exec: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    by_d: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    univ_d: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_d[bjt_day(r)].append(r)
    for r in universe_exec:
        univ_d[bjt_day(r)].append(r)
    out: dict[str, Any] = {}
    for i, day in enumerate(sorted(by_d)):
        rr = by_d[day]
        rand = daily_base.random_same_ts_exec(univ_d.get(day, []), rr, seed + i * 31, min(120, v2.SIMS)) if rr else {}
        ds = daily_base.summarize(rr, len(rr), rand if rand else None)
        out[day] = {
            "n": ds["n"], "avg": ds["avg"], "rand_avg_p95": ds.get("rand_avg_p95", 0.0),
            "edge_avg_p95": ds.get("edge_avg_p95", 0.0), "sum": ds["sum"], "mdd": ds["mdd"],
            "remove_top5_avg": ds["remove_top5_avg"], "initial_stop_rate": ds["initial_stop_rate"],
            "reason_counts": ds["reason_counts"],
        }
    return out


def summarize(name: str, rows: Sequence[Mapping[str, Any]], selected_n: int, universe_exec: Sequence[Mapping[str, Any]], seed: int, miss: Mapping[str, int]) -> dict[str, Any]:
    summ = v2.base.summarize_rows(rows)
    extra = v2.contribution_extra(rows)
    rand = random_same_ts(universe_exec, rows, seed)
    st = summ["stats"]
    c = summ["contribution"]
    daily = daily_edges(rows, universe_exec, seed + 7000)
    rc = extra.get("reason_counts", {})
    passes = {
        "avg_gt_rand_p95": bool(rand and st["avg"] > rand["avg"]["p95"]),
        "sum_gt_rand_p95": bool(rand and st["sum"] > rand["sum"]["p95"]),
        "sh_gt_rand_p95": bool(rand and st["sharpe_like"] > rand["sharpe_like"]["p95"]),
        "mdd_gt_minus5pct": summ["mdd"] >= -0.05,
        "cap5_pos": summ["cap5"]["comp"] > 0,
        "cap10_pos": summ["cap10"]["comp"] > 0,
        "remove_top5_avg_pos": c["remove_top5"]["avg"] >= 0,
        "initial_stop_le_18pct": extra.get("initial_stop_rate", 1.0) <= 0.18,
        "daily_edge_majority": sum(1 for d in daily.values() if d.get("edge_avg_p95", 0.0) > 0) >= max(1, math.ceil(len(daily) * 0.5)),
    }
    return {
        "gate": name,
        "n": summ["n"], "selected_n": selected_n, "exec_rate": summ["n"] / selected_n if selected_n else 0.0,
        "avg": st["avg"], "sum": st["sum"], "sh": st["sharpe_like"], "mdd": summ["mdd"],
        "cap5": summ["cap5"]["comp"], "cap10": summ["cap10"]["comp"],
        "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0,
        "rand_sum_p95": rand.get("sum", {}).get("p95", 0.0) if rand else 0.0,
        "rand_sh_p95": rand.get("sharpe_like", {}).get("p95", 0.0) if rand else 0.0,
        "edge_avg_p95": st["avg"] - (rand.get("avg", {}).get("p95", 0.0) if rand else 0.0),
        "edge_sum_p95": st["sum"] - (rand.get("sum", {}).get("p95", 0.0) if rand else 0.0),
        "remove_top5_avg": c["remove_top5"]["avg"],
        "days": f"{extra.get('positive_avg_days', 0)}/{extra.get('days', 0)}",
        "daily_edge_days": f"{sum(1 for d in daily.values() if d.get('edge_avg_p95', 0.0) > 0)}/{len(daily)}",
        "initial_stop_rate": extra.get("initial_stop_rate", 0.0),
        "win_rate": extra.get("win_rate", 0.0),
        "avg_mfe": summ["avg_mfe"], "avg_capture": summ["avg_capture_ratio"],
        "reason_counts": rc,
        "reason_avg": extra.get("reason_avg", {}),
        "top_symbols": c.get("top_symbols", [])[:8],
        "misses": dict(miss),
        "passes": passes,
        "pass_n": sum(1 for v in passes.values() if v),
        "daily": daily,
    }


def main() -> int:
    pools, pool_meta = v2.build_pools()
    universe, selected = pools[POOL]
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    selected_ann, gate_meta = gate.annotate_with_new_radar(selected, new_rows)
    universe_ann, universe_gate_meta = gate.annotate_with_new_radar(universe, new_rows)

    gates: dict[str, list[Mapping[str, Any]]] = {
        "base_all_C_cd60": list(selected_ann),
        "risk_BTC15_nonnegative": [r for r in selected_ann if btc_nonneg(r)],
        "quality_BTC15_nonneg_AND_prev15_new_mkt_top20": [r for r in selected_ann if quality_gate(r)],
        "anti_chase_BTC15_nonneg_AND_not_prev60_mkt_top10": [r for r in selected_ann if btc_nonneg(r) and not bool(r.get("new_prev60_mkt_top10"))],
    }
    all_rows: list[Mapping[str, Any]] = list(universe_ann) + list(selected_ann)
    bars, bars_meta = v2.base.load_bars_by_symbol(all_rows, max_minutes=95)

    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Validate non-grid execution ideas for Lana old/new radar: early adverse filters, trailing exits after first MFE capture, and causal regime-split exits. Discovery frozen at C_oldcore_cd60; entry fixed PB07_w20; all-taker 8bp; same-ts random over ungated annotated C universe.",
        "settings": {"pool": POOL, "entry": ENTRY.__dict__, "sims": v2.SIMS, "cap_per_ts": v2.CAP_PER_TS, "slim_per_ts": v2.SLIM_PER_TS, "cost": COST},
        "meta": {"pool": pool_meta, "new": new_meta, "gate": gate_meta, "universe_gate": universe_gate_meta, "bars": bars_meta, "gate_counts": {k: len(v) for k, v in gates.items()}},
        "leaderboard": [],
        "by_gate": {},
    }

    universe_exec_cache: dict[str, list[dict[str, Any]]] = {}
    for si, spec in enumerate(SPECS):
        univ_exec, univ_miss = simulate_rows(universe_ann, bars, spec)
        universe_exec_cache[spec.name] = univ_exec
        for gi, (gate_name, sig_rows) in enumerate(gates.items()):
            exec_rows, miss = simulate_rows(sig_rows, bars, spec)
            if len(exec_rows) < 20:
                continue
            s = summarize(gate_name, exec_rows, len(sig_rows), univ_exec, 2026060400 + si * 101 + gi * 17, miss)
            s["exit"] = spec.name
            s["universe_exec_n"] = len(univ_exec)
            s["universe_misses"] = univ_miss
            result["leaderboard"].append(s)
            result["by_gate"].setdefault(gate_name, []).append(s)

    sort_key: Callable[[Mapping[str, Any]], tuple[Any, ...]] = lambda r: (
        r["pass_n"], r["edge_avg_p95"], r["avg"], r["remove_top5_avg"], -abs(r["mdd"]), r["daily_edge_days"]
    )
    result["leaderboard"] = sorted(result["leaderboard"], key=sort_key, reverse=True)
    for k in list(result["by_gate"]):
        result["by_gate"][k] = sorted(result["by_gate"][k], key=sort_key, reverse=True)

    lines: list[str] = []
    lines.append("# Dynamic exit alpha validation")
    lines.append("")
    lines.append(f"generated_utc: `{result['generated_utc']}`")
    lines.append("")
    lines.append(result["method"])
    lines.append("")
    lines.append("## Coverage")
    lines.append("```text")
    pc = pool_meta["counts"][POOL]
    lines.append(f"{POOL}: selected_cap={pc['selected_cap']} slim_universe={pc['slim_universe']} symbols={pc['symbols']} ts={pc['timestamps']}")
    lines.append(f"gate_counts={result['meta']['gate_counts']}")
    lines.append(f"bars_symbols={bars_meta.get('symbols')} bars_errors={len(bars_meta.get('errors', {}))}")
    lines.append("```")
    lines.append("")
    lines.append("## Top candidates")
    lines.append("```text")
    for r in result["leaderboard"][:60]:
        lines.append(
            f"{r['gate']:<48} {r['exit']:<32} pass={r['pass_n']}/9 n={r['n']:3d}/{r['selected_n']:<3d} "
            f"avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} "
            f"sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} "
            f"mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']):>8} cap10={pct(r['cap10']):>8} remT5={pct(r['remove_top5_avg']):>8} "
            f"dEdge={r['daily_edge_days']} stop={r['initial_stop_rate']*100:4.1f}% win={r['win_rate']*100:4.1f}% capR={r['avg_capture']:.2f} reasons={r['reason_counts']} miss={r['misses']}"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Best by gate")
    for gate_name, rows in result["by_gate"].items():
        lines.append(f"### {gate_name}")
        lines.append("```text")
        for r in rows[:10]:
            lines.append(
                f"{r['exit']:<32} pass={r['pass_n']}/9 n={r['n']:3d}/{r['selected_n']:<3d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} "
                f"edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f} "
                f"mdd={pct(r['mdd']):>8} remT5={pct(r['remove_top5_avg']):>8} dEdge={r['daily_edge_days']} stop={r['initial_stop_rate']*100:4.1f}% reasons={r['reason_counts']}"
            )
        lines.append("```")
        lines.append("")
    lines.append("## Daily for top 6")
    for r in result["leaderboard"][:6]:
        lines.append(f"### {r['gate']} / {r['exit']}")
        lines.append("```text")
        for day, d in r["daily"].items():
            lines.append(
                f"{day} n={d['n']:3d} avg={pct(d['avg']):>8} rand95={pct(d['rand_avg_p95']):>8} edge={pct(d['edge_avg_p95']):>8} "
                f"sum={pct(d['sum']):>8} mdd={pct(d['mdd']):>8} remT5={pct(d['remove_top5_avg']):>8} stop={d['initial_stop_rate']*100:4.1f}% reasons={d['reason_counts']}"
            )
        lines.append("```")
        lines.append("")

    out = OUT / f"dynamic-exit-alpha-validation-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    (OUT / "dynamic-exit-alpha-validation-latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    (OUT / "dynamic-exit-alpha-validation-latest.md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    print(OUT / "dynamic-exit-alpha-validation-latest.json")
    print(OUT / "dynamic-exit-alpha-validation-latest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
