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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_execution_alpha_narrow as base  # noqa: E402
import validate_full_data_first_layer_discovery_fast_clean as old_base  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import candidate_top_fraction_by_ts  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, load_snapshot_rows, pct, stat  # noqa: E402

BJ = timezone(timedelta(hours=8))
OUT = PROJECT_ROOT / "output"
SIMS = 50
CAP_PER_TS = 10
SLIM_PER_TS = 40
CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))
DAWN_HOURS = set(range(4, 8))
DAY_HOURS = set(range(8, 20))
NO_TOXIC_23 = set([20, 21, 22, 0, 1, 2, 3])

@dataclass(frozen=True)
class Entry2:
    name: str
    kind: str
    pullback: float = 0.0
    watch_minutes: int = 15
    reclaim: bool = False

ENTRIES = [
    Entry2("N1_next1m", "next1m"),
    Entry2("N5_next5m", "next5m"),
    Entry2("PB03_w10", "pullback", pullback=0.003, watch_minutes=10),
    Entry2("PB03_w20", "pullback", pullback=0.003, watch_minutes=20),
    Entry2("PB05_w15", "pullback", pullback=0.005, watch_minutes=15),
    Entry2("PB05_w25", "pullback", pullback=0.005, watch_minutes=25),
    Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20),
    Entry2("PB10_w25", "pullback", pullback=0.010, watch_minutes=25),
]

EXITS = [
    base.ExitSpec("X0_tp06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20),
    base.ExitSpec("X1_tp08_16_BE_30m", tp1=0.008, tp2=0.016, tp1_size=0.5, lock=0.0, sl=-0.012, time_minutes=30),
    base.ExitSpec("X2_tp10_20_BE_30m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.0, sl=-0.015, time_minutes=30),
    base.ExitSpec("X3_tp12_24_L01_45m", tp1=0.012, tp2=0.024, tp1_size=0.5, lock=0.001, sl=-0.018, time_minutes=45),
    base.ExitSpec("X4_tp10_20_L02_45m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.002, sl=-0.015, time_minutes=45),
    base.ExitSpec("X5_fulltp10_30m", tp1=0.010, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.015, time_minutes=30, full_tp=True),
]


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def filter_hours(rows: Sequence[Mapping[str, Any]], hours: set[int]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows if int(r.get("hour_bjt") or 0) in hours]


def cap_by_ts(rows: Sequence[Mapping[str, Any]], cap: int = CAP_PER_TS) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        out.extend(sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("entry_trigger_score")), -_num(r.get("momentum_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))))[:cap])
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


def slim_universe(universe: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], per_ts: int = SLIM_PER_TS) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in universe:
        by[r["ts_dt"]].append(dict(r))
    out: dict[tuple[datetime, str], dict[str, Any]] = {}
    for ts, group in by.items():
        ranked = sorted(group, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("entry_trigger_score")), -_num(r.get("momentum_confirmation_score")), -_num(r.get("final_score")), str(r.get("symbol"))))[:per_ts]
        for r in ranked:
            out[(ts, str(r.get("symbol")))] = dict(r)
    for r in selected:
        out[(r["ts_dt"], str(r.get("symbol")))] = dict(r)
    return list(out.values())


def top_fraction(rows: Sequence[Mapping[str, Any]], key: str, frac: float = 0.20) -> list[dict[str, Any]]:
    return candidate_top_fraction_by_ts([dict(r) for r in rows], key, frac)


def ceil_next_minute(dt: datetime) -> datetime:
    return base.ceil_next_minute(dt)


def decide_entry2(bars: Sequence[Sequence[Any]], signal_dt: datetime, spec: Entry2) -> tuple[datetime | None, float | None, str]:
    if not bars:
        return None, None, "no_bars"
    if spec.kind == "next1m":
        base_dt = base.ceil_next_minute(signal_dt)
        b = base.first_bar_at_or_after(bars, base_dt)
        return (base.bar_dt(b), float(b[1]), "entry_now") if b else (None, None, "no_base_entry")
    if spec.kind == "next5m":
        base_dt = base.ceil_next_5m(signal_dt)
        b = base.first_bar_at_or_after(bars, base_dt)
        return (base.bar_dt(b), float(b[1]), "entry_now") if b else (None, None, "no_base_entry")
    start = base.ceil_next_minute(signal_dt)
    ref = base.first_bar_at_or_after(bars, start)
    if ref is None:
        return None, None, "no_ref"
    ref_px = float(ref[1])
    if ref_px <= 0:
        return None, None, "bad_ref_px"
    target = ref_px * (1.0 - spec.pullback)
    watch = base._bars_from(bars, start, start + timedelta(minutes=spec.watch_minutes))
    if not watch:
        return None, None, "no_watch"
    for b in watch:
        if float(b[3]) <= target:
            return base.bar_dt(b), target, "pullback_entry"
    return None, None, "no_pullback"


def simulate_combo2(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], es: Entry2, xs: base.ExitSpec) -> tuple[list[dict[str, Any]], Counter[str]]:
    out: list[dict[str, Any]] = []
    misses: Counter[str] = Counter()
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        entry_dt, entry_px, er = decide_entry2(bars, r["ts_dt"], es)
        r["entry_rule"] = es.name
        r["exit_rule"] = xs.name
        r["entry_reason"] = er
        if entry_dt is None or entry_px is None:
            misses[er] += 1
            continue
        sim = base.simulate_exit(bars, entry_dt, entry_px, xs)
        if sim.get("pnl") is None:
            misses[str(sim.get("reason"))] += 1
            continue
        r.update(sim)
        r["entry_dt"] = iso(entry_dt)
        r["entry_price"] = entry_px
        out.append(r)
    return out, misses


def contribution_extra(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    c = base.contribution(rows)
    vals = [float(r["pnl"]) for r in rows]
    wins = sum(1 for v in vals if v > 0)
    reasons = Counter(str(r.get("reason")) for r in rows)
    init_stop = reasons.get("initial_stop", 0) / len(rows) if rows else 0.0
    return {**c, "win_rate": wins / len(rows) if rows else 0.0, "reason_counts": dict(reasons), "initial_stop_rate": init_stop}


def random_same_ts(universe_exec: Sequence[Mapping[str, Any]], selected_exec: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    if not selected_exec or len(selected_exec) >= len(universe_exec):
        return {}
    u_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    s_by: defaultdict[datetime, list[Mapping[str, Any]]] = defaultdict(list)
    for r in universe_exec:
        u_by[r["ts_dt"]].append(r)
    for r in selected_exec:
        s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    shs: list[float] = []
    for _ in range(SIMS):
        picked: list[float] = []
        for ts, sg in s_by.items():
            pool = [r for r in u_by.get(ts, []) if r.get("pnl") is not None]
            if not pool:
                continue
            pool = sorted(pool, key=lambda r: (-_num(r.get("market_confirmation_score")), str(r.get("symbol"))))[:SLIM_PER_TS]
            n = len(sg)
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


def build_pools() -> tuple[dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]], dict[str, Any]]:
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    old_rows, old_meta = old_base.load_old_signal_scores()

    # Keep new radar tests on its own full available window; old C variants are truncated to new window for direct comparison.
    min_new = min(r["ts_dt"] for r in new_rows)
    max_new = max(r["ts_dt"] for r in new_rows)
    old_overlap = [dict(r) for r in old_rows if min_new <= r["ts_dt"] <= max_new]

    A_mkt = top_fraction(new_rows, "market_confirmation_score", 0.20)
    A_entry = top_fraction(new_rows, "entry_trigger_score", 0.20)
    A_momo = top_fraction(new_rows, "momentum_confirmation_score", 0.20)
    # Causal blend: not an outcome. Uses scores available in snapshot rows.
    blend_rows = []
    for r0 in new_rows:
        r = dict(r0)
        r["blend_exec_score"] = _num(r.get("market_confirmation_score")) + 0.65 * _num(r.get("entry_trigger_score")) + 0.35 * _num(r.get("momentum_confirmation_score"))
        blend_rows.append(r)
    A_blend = top_fraction(blend_rows, "blend_exec_score", 0.20)

    old_core = [dict(r) for r in old_overlap if str(r.get("session")) == "core_night"]
    C = top_fraction(old_core, "market_confirmation_score", 0.20)
    C_no23 = filter_hours(C, NO_TOXIC_23)
    C_cd60 = cooldown_symbol(C, 60)
    C_no23_cd60 = cooldown_symbol(C_no23, 60)

    raw: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {
        "A_mkt_core": (filter_hours(new_rows, CORE_HOURS), filter_hours(A_mkt, CORE_HOURS)),
        "A_mkt_night": (filter_hours(new_rows, NIGHT_HOURS), filter_hours(A_mkt, NIGHT_HOURS)),
        "A_mkt_dawn": (filter_hours(new_rows, DAWN_HOURS), filter_hours(A_mkt, DAWN_HOURS)),
        "A_entry_core": (filter_hours(new_rows, CORE_HOURS), filter_hours(A_entry, CORE_HOURS)),
        "A_momo_core": (filter_hours(new_rows, CORE_HOURS), filter_hours(A_momo, CORE_HOURS)),
        "A_blend_core": (filter_hours(blend_rows, CORE_HOURS), filter_hours(A_blend, CORE_HOURS)),
        "C_oldcore_base": (old_core, C),
        "C_oldcore_no23": (filter_hours(old_core, NO_TOXIC_23), C_no23),
        "C_oldcore_cd60": (old_core, C_cd60),
        "C_oldcore_no23_cd60": (filter_hours(old_core, NO_TOXIC_23), C_no23_cd60),
    }
    pools = {}
    counts = {}
    for k, (univ, sel) in raw.items():
        sel_cap = cap_by_ts(sel, CAP_PER_TS)
        pools[k] = (slim_universe(univ, sel_cap, SLIM_PER_TS), sel_cap)
        counts[k] = {"raw_universe": len(univ), "slim_universe": len(pools[k][0]), "selected_raw": len(sel), "selected_cap": len(sel_cap), "symbols": len({str(r.get("symbol")) for r in sel_cap}), "timestamps": len({r["ts_dt"] for r in sel_cap})}
    meta = {"new": new_meta, "old": old_meta, "new_window_utc": [iso(min_new), iso(max_new)], "counts": counts, "cap_per_ts": CAP_PER_TS, "slim_per_ts": SLIM_PER_TS, "sims": SIMS}
    return pools, meta


def main() -> int:
    pools, meta = build_pools()
    all_rows: list[dict[str, Any]] = []
    for univ, sel in pools.values():
        all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in EXITS) + 40)

    result: dict[str, Any] = {"generated_utc": iso(datetime.now(timezone.utc)), "method": "Focused v2 execution-alpha search: freeze causal new/old radar pools; test new radar MFE pools and C variants; variable pullback windows; all-taker 8bp; stop-first 1m replay; same-timestamp execution random.", "meta": {"pools": meta, "bars": bars_meta}, "pools": {}, "leaderboard": []}
    combo_idx = 0
    for pool_name, (universe, selected) in pools.items():
        result["pools"][pool_name] = {"universe_n": len(universe), "selected_n": len(selected), "combos": {}}
        for es in ENTRIES:
            for xs in EXITS:
                combo_idx += 1
                key = f"{es.name}__{xs.name}"
                sel_exec, sel_miss = simulate_combo2(selected, bars, es, xs)
                if len(sel_exec) < 20:
                    continue
                univ_exec, _ = simulate_combo2(universe, bars, es, xs)
                summ = base.summarize_rows(sel_exec)
                extra = contribution_extra(sel_exec)
                summ["contribution"] = extra
                rand = random_same_ts(univ_exec, sel_exec, 2026060500 + combo_idx * 37 + len(pool_name))
                c = summ["contribution"]
                passes = {
                    "avg_gt_rand_p95": bool(rand and summ["stats"]["avg"] > rand["avg"]["p95"]),
                    "sum_gt_rand_p95": bool(rand and summ["stats"]["sum"] > rand["sum"]["p95"]),
                    "sh_gt_rand_p95": bool(rand and summ["stats"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
                    "cap5_pos": bool(summ["cap5"]["comp"] > 0),
                    "cap10_pos": bool(summ["cap10"]["comp"] > 0),
                    "remove_top5_avg_pos": bool(c["remove_top5"]["avg"] >= 0),
                    "days_majority_pos": bool(c["positive_avg_days"] >= max(1, math.ceil(c["days"] * 0.5))),
                    "initial_stop_le_25pct": bool(c.get("initial_stop_rate", 1.0) <= 0.25),
                }
                row = {
                    "pool": pool_name,
                    "combo": key,
                    "n": summ["n"],
                    "selected_n": len(selected),
                    "exec_rate": summ["n"] / len(selected) if selected else 0.0,
                    "avg": summ["stats"]["avg"],
                    "sum": summ["stats"]["sum"],
                    "sh": summ["stats"]["sharpe_like"],
                    "mdd": summ["mdd"],
                    "cap5": summ["cap5"]["comp"],
                    "cap10": summ["cap10"]["comp"],
                    "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0,
                    "rand_sum_p95": rand.get("sum", {}).get("p95", 0.0) if rand else 0.0,
                    "rand_sh_p95": rand.get("sharpe_like", {}).get("p95", 0.0) if rand else 0.0,
                    "edge_avg_p95": summ["stats"]["avg"] - (rand.get("avg", {}).get("p95", 0.0) if rand else 0.0),
                    "remove_top5_avg": c["remove_top5"]["avg"],
                    "days": f"{c['positive_avg_days']}/{c['days']}",
                    "initial_stop_rate": c.get("initial_stop_rate", 0.0),
                    "reason_counts": c.get("reason_counts", {}),
                    "missed_entry": dict(sel_miss),
                    "avg_mfe": summ["avg_mfe"],
                    "avg_capture": summ["avg_capture_ratio"],
                    "passes": passes,
                }
                result["pools"][pool_name]["combos"][key] = {"entry": es.__dict__, "exit": xs.__dict__, "selected": summ, "random_same_ts": rand, "passes": passes, "missed_entry": dict(sel_miss), "universe_exec_n": len(univ_exec)}
                result["leaderboard"].append(row)

    result["leaderboard"] = sorted(result["leaderboard"], key=lambda r: (sum(1 for v in r["passes"].values() if v), r["edge_avg_p95"], r["avg"], r["remove_top5_avg"]), reverse=True)
    lines = ["# Focused execution alpha search v2", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Coverage", "```text"]
    lines.append(f"new_window={meta['new_window_utc'][0]} -> {meta['new_window_utc'][1]}")
    for k, c in meta["counts"].items():
        lines.append(f"{k}: raw_universe={c['raw_universe']} slim_universe={c['slim_universe']} selected_raw={c['selected_raw']} selected_cap={c['selected_cap']} symbols={c['symbols']} ts={c['timestamps']}")
    lines.append(f"1m_symbols={bars_meta['symbols']} errors={len(bars_meta['errors'])}")
    lines.extend(["```", "", "## Leaderboard", "", "```text"])
    for r in result["leaderboard"][:60]:
        pass_n = sum(1 for v in r["passes"].values() if v)
        lines.append(f"{r['pool']:<22} {r['combo']:<34} pass={pass_n}/8 n={r['n']:4d}/{r['selected_n']:<4d} ex={r['exec_rate']*100:5.1f}% avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']):>8} cap10={pct(r['cap10']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['days']} stop={r['initial_stop_rate']*100:4.1f}% mfe={pct(r['avg_mfe']):>8} reasons={r['reason_counts']}")
    lines.append("```")
    lines.extend(["", "## Best by pool", ""])
    for pool_name in pools:
        lines.append(f"### {pool_name}")
        lines.append("```text")
        for r in [x for x in result["leaderboard"] if x["pool"] == pool_name][:8]:
            pass_n = sum(1 for v in r["passes"].values() if v)
            lines.append(f"{r['combo']:<34} pass={pass_n}/8 n={r['n']:4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} sum={pct(r['sum']):>8} sh={r['sh']:5.2f} mdd={pct(r['mdd']):>8} remT5={pct(r['remove_top5_avg']):>8} days={r['days']} stop={r['initial_stop_rate']*100:4.1f}% miss={r['missed_entry']}")
        lines.append("```")
        lines.append("")
    out = OUT / f"execution-alpha-focused-v2-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
