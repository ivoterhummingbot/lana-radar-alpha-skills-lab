#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import validate_full_data_first_layer_discovery_fast_clean as clean_base  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import candidate_top_fraction_by_ts  # noqa: E402
from radar_alpha_skills_lab.signal_control import COSTS, iso, load_snapshot_rows, pct, stat  # noqa: E402

BJ = timezone(timedelta(hours=8))
OUT = PROJECT_ROOT / "output"
CACHE = OUT / "klines_1m_cache" / "execution_alpha_search"
COST = float(COSTS.get("all_taker", 0.0008))
SIMS = 40
RANDOM_LIMIT_UNIVERSE_PER_TS = 40  # first-pass speed guard; deterministic top-score same-ts universe

CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))


@dataclass(frozen=True)
class EntrySpec:
    name: str
    kind: str
    pullback: float = 0.0
    break_minutes: int = 5
    skip_pump: float | None = None


@dataclass(frozen=True)
class ExitSpec:
    name: str
    tp1: float
    tp2: float | None
    tp1_size: float
    lock: float
    sl: float
    time_minutes: int
    full_tp: bool = False


ENTRIES = [
    EntrySpec("E0_next1m", "next1m"),
    EntrySpec("E1_next5m", "next5m"),
    EntrySpec("E2_pullback05", "pullback", pullback=0.005),
    EntrySpec("E3_pullback10", "pullback", pullback=0.010),
    EntrySpec("E4_pb05_break5m", "pullback_break", pullback=0.005, break_minutes=5),
    EntrySpec("E5_skip5mPump1_next5m", "next5m", skip_pump=0.010),
]

EXITS = [
    ExitSpec("X1_tp10_20_BE_30m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.0, sl=-0.015, time_minutes=30),
    ExitSpec("X2_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45),
    ExitSpec("X3_fulltp10_30m", tp1=0.010, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.015, time_minutes=30, full_tp=True),
    ExitSpec("X4_fulltp15_60m", tp1=0.015, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.020, time_minutes=60, full_tp=True),
    ExitSpec("X5_tp08_16_BE_30m", tp1=0.008, tp2=0.016, tp1_size=0.5, lock=0.0, sl=-0.012, time_minutes=30),
]


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def ceil_next_minute(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if dt.second == 0 and dt.microsecond == 0:
        return dt
    return (dt.replace(second=0, microsecond=0) + timedelta(minutes=1))


def ceil_next_5m(dt: datetime) -> datetime:
    dt = ceil_next_minute(dt)
    minute = ((dt.minute + 4) // 5) * 5
    if minute >= 60:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt.replace(minute=minute, second=0, microsecond=0)


def bar_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def fetch_1m_chunked(raw_symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    """Fetch 1m klines safely under Binance 1500 limit, with local cache per requested range."""
    CACHE.mkdir(parents=True, exist_ok=True)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cache = CACHE / f"{raw_symbol}-1m-{start_ms}-{end_ms}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out: list[list[Any]] = []
    cur = start
    while cur < end:
        chunk_end = min(end, cur + timedelta(minutes=1490))
        query = urllib.parse.urlencode({"symbol": raw_symbol, "interval": "1m", "startTime": int(cur.timestamp()*1000), "endTime": int(chunk_end.timestamp()*1000), "limit": 1500})
        req = urllib.request.Request("https://fapi.binance.com/fapi/v1/klines?" + query, headers={"User-Agent": "lana-radar-alpha-skills-lab/exec-alpha/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            raise RuntimeError(str(data)[:200])
        out.extend(data)
        if not data:
            break
        last_open = datetime.fromtimestamp(int(data[-1][0]) / 1000, tz=timezone.utc)
        nxt = last_open + timedelta(minutes=1)
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.02)
    # de-dupe by open time
    dedup = {int(b[0]): b for b in out}
    rows = [dedup[k] for k in sorted(dedup)]
    cache.write_text(json.dumps(rows))
    return rows


def load_bars_by_symbol(rows: Sequence[Mapping[str, Any]], max_minutes: int = 75) -> tuple[dict[str, list[list[Any]]], dict[str, Any]]:
    if not rows:
        return {}, {"symbols": 0, "errors": {}}
    start = min(r["ts_dt"] for r in rows) - timedelta(minutes=5)
    end = max(r["ts_dt"] for r in rows) + timedelta(minutes=max_minutes + 20)
    bars: dict[str, list[list[Any]]] = {}
    errors: dict[str, str] = {}
    for raw in sorted({str(r["raw_symbol"]) for r in rows if r.get("raw_symbol")}):
        try:
            bars[raw] = fetch_1m_chunked(raw, start, end)
        except Exception as exc:  # noqa: BLE001
            errors[raw] = str(exc)[:240]
            bars[raw] = []
    return bars, {"symbols": len(bars), "errors": errors, "fetch_start_utc": iso(start), "fetch_end_utc": iso(end)}


def _bars_from(bars: Sequence[Sequence[Any]], start: datetime, end: datetime) -> list[Sequence[Any]]:
    s = int(start.timestamp() * 1000)
    e = int(end.timestamp() * 1000)
    return [b for b in bars if s <= int(b[0]) <= e]


def first_bar_at_or_after(bars: Sequence[Sequence[Any]], dt: datetime) -> Sequence[Any] | None:
    ms = int(dt.timestamp() * 1000)
    for b in bars:
        if int(b[0]) >= ms:
            return b
    return None


def decide_entry(bars: Sequence[Sequence[Any]], signal_dt: datetime, spec: EntrySpec) -> tuple[datetime | None, float | None, str]:
    if not bars:
        return None, None, "no_bars"
    base_dt = ceil_next_minute(signal_dt) if spec.kind == "next1m" else ceil_next_5m(signal_dt)
    base_bar = first_bar_at_or_after(bars, base_dt)
    if base_bar is None:
        return None, None, "no_base_entry"
    base_px = float(base_bar[1])
    if base_px <= 0:
        return None, None, "bad_base_px"

    # skip if first 5m already pumped too much from base open
    if spec.skip_pump is not None:
        look = _bars_from(bars, base_dt, base_dt + timedelta(minutes=5))
        if look and max(float(b[2]) for b in look) / base_px - 1.0 >= spec.skip_pump:
            return None, None, "skip_5m_pump"

    if spec.kind in {"next1m", "next5m"}:
        return bar_dt(base_bar), base_px, "entry_now"

    watch_end = ceil_next_minute(signal_dt) + timedelta(minutes=15)
    watch = _bars_from(bars, ceil_next_minute(signal_dt), watch_end)
    if not watch:
        return None, None, "no_watch"
    ref_bar = first_bar_at_or_after(bars, ceil_next_minute(signal_dt))
    if ref_bar is None:
        return None, None, "no_ref"
    ref_px = float(ref_bar[1])
    target = ref_px * (1.0 - spec.pullback)
    hit_dt = None
    hit_px = None
    for b in watch:
        if float(b[3]) <= target:
            hit_dt = bar_dt(b)
            hit_px = target
            break
    if hit_dt is None or hit_px is None:
        return None, None, "no_pullback"
    if spec.kind == "pullback":
        return hit_dt, hit_px, "pullback_entry"
    # pullback then break above local high of preceding break window, within next 20m
    pre_start = max(ceil_next_minute(signal_dt), hit_dt - timedelta(minutes=spec.break_minutes))
    pre = _bars_from(bars, pre_start, hit_dt)
    if not pre:
        return None, None, "no_pre_break"
    break_px = max(float(b[2]) for b in pre)
    for b in _bars_from(bars, hit_dt, hit_dt + timedelta(minutes=20)):
        if float(b[2]) >= break_px:
            return bar_dt(b), break_px, "pullback_break_entry"
    return None, None, "no_break_after_pullback"


def simulate_exit(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, xs: ExitSpec) -> dict[str, Any]:
    if entry_px <= 0:
        return {"pnl": None, "reason": "bad_entry"}
    end = entry_dt + timedelta(minutes=xs.time_minutes)
    path = _bars_from(bars, entry_dt, end)
    if not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    tp1_px = entry_px * (1 + xs.tp1)
    tp2_px = entry_px * (1 + xs.tp2) if xs.tp2 is not None else None
    sl_px = entry_px * (1 + xs.sl)
    lock_px = entry_px * (1 + xs.lock)
    pnl = 0.0
    left = 1.0
    tp1_hit = False
    for b in path:
        high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
        # conservative priority: stop before target inside same minute unless already protected.
        if tp1_hit:
            if low <= lock_px:
                pnl += left * xs.lock
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "lock_after_tp1", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
        else:
            if low <= sl_px:
                pnl = xs.sl
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "initial_stop", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
        if not tp1_hit and high >= tp1_px:
            pnl += xs.tp1_size * xs.tp1
            left -= xs.tp1_size
            tp1_hit = True
            if xs.full_tp or left <= 1e-9:
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp1_full", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
        if tp2_px is not None and high >= tp2_px:
            pnl += left * xs.tp2
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp2_after_tp1" if tp1_hit else "tp2_full", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
        if close_ms >= int(end.timestamp() * 1000):
            pnl += left * (close / entry_px - 1.0)
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp1_then_time" if tp1_hit else "time_exit", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}
    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1.0)
    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp1_then_time" if tp1_hit else "time_exit", "mfe": mfe, "mae": mae, "capture_ratio": (pnl - COST) / mfe if mfe > 0 else 0.0}


def simulate_combo(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], es: EntrySpec, xs: ExitSpec) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        entry_dt, entry_px, er = decide_entry(bars, r["ts_dt"], es)
        r["entry_rule"] = es.name
        r["exit_rule"] = xs.name
        r["entry_reason"] = er
        if entry_dt is None or entry_px is None:
            continue
        sim = simulate_exit(bars, entry_dt, entry_px, xs)
        if sim.get("pnl") is None:
            continue
        r.update(sim)
        r["entry_dt"] = iso(entry_dt)
        r["entry_price"] = entry_px
        out.append(r)
    return out


def cap_portfolio(rows: Sequence[Mapping[str, Any]], cap: int) -> dict[str, float]:
    by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(r)
    picked: list[float] = []
    for _ts, group in sorted(by.items()):
        ranked = sorted(group, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))))[:cap]
        picked.extend(float(r["pnl"]) for r in ranked)
    comp = 1.0
    for v in picked:
        comp *= 1.0 + v
    return {"n": len(picked), "sum": sum(picked), "avg": sum(picked)/len(picked) if picked else 0.0, "comp": comp - 1.0}


def max_drawdown(vals: Sequence[float]) -> float:
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for v in vals:
        eq *= 1.0 + v
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1.0)
    return mdd


def contribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_sym: defaultdict[str, list[float]] = defaultdict(list)
    by_day: defaultdict[str, list[float]] = defaultdict(list)
    by_reason: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = float(r["pnl"])
        by_sym[str(r.get("symbol"))].append(v)
        by_day[str(r.get("date_bjt"))].append(v)
        by_reason[str(r.get("reason"))].append(v)
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    rem5 = {s for s, _v, _n in ranked[:5]}
    return {
        "top_symbols": ranked[:10],
        "remove_top5": stat(float(r["pnl"]) for r in rows if str(r.get("symbol")) not in rem5),
        "day_avg": {d: sum(v)/len(v) for d, v in sorted(by_day.items()) if v},
        "day_sum": {d: sum(v) for d, v in sorted(by_day.items()) if v},
        "positive_avg_days": sum(1 for v in by_day.values() if v and sum(v)/len(v) > 0),
        "days": len(by_day),
        "reason_counts": {k: len(v) for k, v in sorted(by_reason.items())},
        "reason_avg": {k: sum(v)/len(v) for k, v in sorted(by_reason.items()) if v},
    }


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vals = [float(r["pnl"]) for r in rows]
    st = stat(vals)
    avg_mfe = sum(float(r.get("mfe") or 0.0) for r in rows) / len(rows) if rows else 0.0
    avg_cap = sum(float(r.get("capture_ratio") or 0.0) for r in rows if float(r.get("mfe") or 0.0) > 0) / max(1, sum(1 for r in rows if float(r.get("mfe") or 0.0) > 0))
    return {"n": len(rows), "stats": st, "mdd": max_drawdown(vals), "avg_mfe": avg_mfe, "avg_capture_ratio": avg_cap, "cap5": cap_portfolio(rows, 5), "cap10": cap_portfolio(rows, 10), "contribution": contribution(rows)}


def group_by_ts(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    return by


def random_same_ts_execution(universe_exec: Sequence[Mapping[str, Any]], selected_exec: Sequence[Mapping[str, Any]], seed: int) -> dict[str, dict[str, float]]:
    if not selected_exec or len(selected_exec) >= len(universe_exec):
        return {}
    u_by = group_by_ts(universe_exec)
    counts = {ts: len(g) for ts, g in group_by_ts(selected_exec).items()}
    rng = random.Random(seed)
    avgs: list[float] = []; sums: list[float] = []; shs: list[float] = []
    for _ in range(SIMS):
        picked: list[float] = []
        for ts, n in counts.items():
            pool = [r for r in u_by.get(ts, []) if r.get("pnl") is not None]
            if not pool:
                continue
            if len(pool) > RANDOM_LIMIT_UNIVERSE_PER_TS:
                pool = sorted(pool, key=lambda r: (-_num(r.get("market_confirmation_score")), str(r.get("symbol"))))[:RANDOM_LIMIT_UNIVERSE_PER_TS]
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            picked.extend(float(r["pnl"]) for r in sample)
        st = stat(picked)
        avgs.append(st["avg"]); sums.append(st["sum"]); shs.append(st["sharpe_like"])
    def q(xs: Sequence[float], p: float) -> float:
        if not xs:
            return 0.0
        ys = sorted(xs)
        return ys[min(len(ys)-1, max(0, int(round((len(ys)-1)*p))))]
    return {"avg": {"p50": q(avgs, .5), "p95": q(avgs, .95)}, "sum": {"p50": q(sums, .5), "p95": q(sums, .95)}, "sharpe_like": {"p50": q(shs, .5), "p95": q(shs, .95)}, "sims": SIMS}


def filter_hour(rows: Sequence[Mapping[str, Any]], hours: set[int]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows if int(r.get("hour_bjt") or 0) in hours]


def slim_universe(universe: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], per_ts: int = RANDOM_LIMIT_UNIVERSE_PER_TS) -> list[dict[str, Any]]:
    """Keep selected plus top-score same-timestamp rows for random baseline speed."""
    selected_keys = {(r["ts_dt"], str(r.get("symbol"))) for r in selected}
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


def load_pools() -> tuple[dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]], dict[str, Any]]:
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    old_rows, old_meta = clean_base.load_old_signal_scores()
    # Use overlap with new radar availability for all pools, so P1/P3 and old pools are comparable and not stale-skewed.
    min_new = min(r["ts_dt"] for r in new_rows)
    max_new = max(r["ts_dt"] for r in new_rows)
    old_rows = [r for r in old_rows if min_new <= r["ts_dt"] <= max_new]

    A_all = candidate_top_fraction_by_ts(new_rows, "market_confirmation_score", 0.20)
    A_core = filter_hour(A_all, CORE_HOURS)
    A_night = filter_hour(A_all, NIGHT_HOURS)
    B_all = [dict(r) for r in old_rows if str(r.get("decision_status")) == "watch_hot"]
    B_night = filter_hour(B_all, NIGHT_HOURS)
    old_core_universe = [dict(r) for r in old_rows if str(r.get("session")) == "core_night"]
    C = candidate_top_fraction_by_ts(old_core_universe, "market_confirmation_score", 0.20)

    b_by_sym: defaultdict[str, list[datetime]] = defaultdict(list)
    for r in B_all:
        b_by_sym[str(r.get("symbol"))].append(r["ts_dt"])
    AB_near = [dict(r) for r in A_night if any(abs((r["ts_dt"] - t).total_seconds()) <= 1800 for t in b_by_sym.get(str(r.get("symbol")), []))]

    raw_pools = {
        "P1_ABnear30_night": (filter_hour(new_rows, NIGHT_HOURS), AB_near),
        "P2_B_watchhot_night": (filter_hour(old_rows, NIGHT_HOURS), B_night),
        "P3_A_market_core": (filter_hour(new_rows, CORE_HOURS), A_core),
        "P4_C_oldcore_mfe": (old_core_universe, C),
    }
    pools = {k: (slim_universe(univ, sel), sel) for k, (univ, sel) in raw_pools.items()}
    meta = {
        "new": new_meta,
        "old": old_meta,
        "overlap_start_utc": iso(min_new),
        "overlap_end_utc": iso(max_new),
        "universe_slim": f"top {RANDOM_LIMIT_UNIVERSE_PER_TS} by timestamp plus selected",
        "counts": {k: {"raw_universe": len(raw_pools[k][0]), "slim_universe": len(v[0]), "selected": len(v[1])} for k, v in pools.items()},
    }
    return pools, meta


def fmt_pct(x: float) -> str:
    return pct(float(x))


def main() -> int:
    pools, meta = load_pools()
    all_rows_for_bars: list[dict[str, Any]] = []
    for univ, sel in pools.values():
        all_rows_for_bars.extend(univ)
        all_rows_for_bars.extend(sel)
    bars_by_symbol, bars_meta = load_bars_by_symbol(all_rows_for_bars, max_minutes=max(x.time_minutes for x in EXITS) + 35)
    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Execution alpha first narrow search. Fixed discovery pools only; 1m post-signal execution replay; entries next/pullback; exits fast TP/protect/time-stop; all-taker 8bp; same-timestamp execution random; overlap period only; no radar refit.",
        "cost": COST,
        "sims": SIMS,
        "meta": {"pools": meta, "bars": bars_meta},
        "pools": {},
        "leaderboard": [],
    }
    lines = ["# Execution alpha narrow search", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Coverage", "```text"]
    lines.append(f"overlap={meta['overlap_start_utc']} -> {meta['overlap_end_utc']}")
    for k, c in meta["counts"].items():
        lines.append(f"{k}: raw_universe={c['raw_universe']} slim_universe={c['slim_universe']} selected={c['selected']}")
    lines.append(f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}")
    lines.extend(["```", "", "## Top candidates", "", "```text"])

    combo_idx = 0
    for pool_name, (universe, selected) in pools.items():
        result["pools"][pool_name] = {"universe_n": len(universe), "selected_n": len(selected), "combos": {}}
        for es in ENTRIES:
            for xs in EXITS:
                combo_idx += 1
                key = f"{es.name}__{xs.name}"
                sel_exec = simulate_combo(selected, bars_by_symbol, es, xs)
                if len(sel_exec) < 20:
                    continue
                univ_exec = simulate_combo(universe, bars_by_symbol, es, xs)
                summ = summarize_rows(sel_exec)
                rand = random_same_ts_execution(univ_exec, sel_exec, 2026060400 + combo_idx * 97)
                c = summ["contribution"]
                passes = {
                    "avg_gt_rand_p95": bool(rand and summ["stats"]["avg"] > rand["avg"]["p95"]),
                    "sum_gt_rand_p95": bool(rand and summ["stats"]["sum"] > rand["sum"]["p95"]),
                    "sh_gt_rand_p95": bool(rand and summ["stats"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
                    "cap5_pos": bool(summ["cap5"]["comp"] > 0),
                    "cap10_pos": bool(summ["cap10"]["comp"] > 0),
                    "remove_top5_avg_pos": bool(c["remove_top5"]["avg"] >= 0),
                    "days_majority_pos": bool(c["positive_avg_days"] >= max(1, math.ceil(c["days"] * 0.5))),
                }
                score = sum(1 for v in passes.values() if v) + max(0.0, summ["stats"]["avg"] * 100) + max(0.0, c["remove_top5"]["avg"] * 100)
                block = {"entry": es.__dict__, "exit": xs.__dict__, "selected": summ, "random_same_ts": rand, "passes": passes, "score": score, "universe_exec_n": len(univ_exec)}
                result["pools"][pool_name]["combos"][key] = block
                result["leaderboard"].append({"pool": pool_name, "combo": key, "score": score, "n": summ["n"], "avg": summ["stats"]["avg"], "sum": summ["stats"]["sum"], "sh": summ["stats"]["sharpe_like"], "mdd": summ["mdd"], "cap5": summ["cap5"]["comp"], "cap10": summ["cap10"]["comp"], "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0, "remove_top5_avg": c["remove_top5"]["avg"], "days": f"{c['positive_avg_days']}/{c['days']}", "passes": passes, "reason_counts": c["reason_counts"], "avg_capture": summ["avg_capture_ratio"], "avg_mfe": summ["avg_mfe"]})

    result["leaderboard"] = sorted(result["leaderboard"], key=lambda x: (sum(1 for v in x["passes"].values() if v), x["score"], x["avg"]), reverse=True)
    for row in result["leaderboard"][:30]:
        pass_n = sum(1 for v in row["passes"].values() if v)
        lines.append(f"{row['pool']:<22} {row['combo']:<42} pass={pass_n}/7 n={row['n']:4d} avg={fmt_pct(row['avg']):>8} rand95={fmt_pct(row['rand_avg_p95']):>8} sum={fmt_pct(row['sum']):>8} sh={row['sh']:6.2f} mdd={fmt_pct(row['mdd']):>8} cap5={fmt_pct(row['cap5']):>8} cap10={fmt_pct(row['cap10']):>8} remT5={fmt_pct(row['remove_top5_avg']):>8} days={row['days']} capRatio={row['avg_capture']:.2f} mfe={fmt_pct(row['avg_mfe']):>8} reasons={row['reason_counts']}")
    lines.extend(["```", "", "## Best by pool", ""])
    for pool_name in pools:
        best = [r for r in result["leaderboard"] if r["pool"] == pool_name][:8]
        lines.append(f"### {pool_name}")
        lines.append("```text")
        for row in best:
            pass_n = sum(1 for v in row["passes"].values() if v)
            lines.append(f"{row['combo']:<42} pass={pass_n}/7 n={row['n']:4d} avg={fmt_pct(row['avg']):>8}/{fmt_pct(row['rand_avg_p95']):>8} sum={fmt_pct(row['sum']):>8} cap5={fmt_pct(row['cap5']):>8} cap10={fmt_pct(row['cap10']):>8} remT5={fmt_pct(row['remove_top5_avg']):>8} days={row['days']} reasons={row['reason_counts']}")
        lines.append("```")
        lines.append("")
    out = OUT / f"execution-alpha-narrow-search-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
