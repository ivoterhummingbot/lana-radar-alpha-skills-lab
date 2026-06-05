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

import search_old_execution_alpha_second_stage as s2  # noqa: E402
from radar_alpha_skills_lab.old_radar_alpha import load_old_radar_rows  # noqa: E402
from radar_alpha_skills_lab.signal_control import COSTS, iso, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
COST = float(COSTS.get("all_taker", 0.0008))
# Fast first-pass: full-window path simulation is expensive; use small random sims for
# search, then validate the short list. Increase only after a candidate passes.
SEARCH_SIMS = 25
VALIDATE_SIMS = 200

CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))

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
    size1: float = 0.5
    lock: float = 0.0
    sl: float = -0.02
    time_minutes: int = 60
    activation: float | None = None
    trail: float | None = None

ENTRIES = [
    Entry("N5", "next5m"),
    Entry("PB05_w30", "pullback", 0.005, 30),
    Entry("PB10_w60", "pullback", 0.010, 60),
]

EXITS = [
    Exit("P10_20_L02_120", "static", tp1=0.010, tp2=0.020, lock=0.002, sl=-0.018, time_minutes=120),
    Exit("P15_30_L05_240", "static", tp1=0.015, tp2=0.030, lock=0.005, sl=-0.025, time_minutes=240),
    Exit("TR12_T07_180", "trail", activation=0.012, trail=0.007, sl=-0.018, time_minutes=180, lock=0.002),
    Exit("TR20_T10_360", "trail", activation=0.020, trail=0.010, sl=-0.030, time_minutes=360, lock=0.004),
    Exit("HOLD4H_SL35", "hold", sl=-0.035, time_minutes=240),
]


def _num(x: Any) -> float:
    try:
        v = float(x or 0.0)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def bjt_day(r: Mapping[str, Any]) -> str:
    dt = r.get("ts_dt")
    if isinstance(dt, datetime):
        return dt.astimezone(BJ).date().isoformat()
    return str(r.get("date_bjt") or "")


def top_frac(rows: Sequence[Mapping[str, Any]], key: str, frac: float) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        n = max(1, math.ceil(len(g) * frac))
        out.extend(sorted(g, key=lambda r: (-_num(r.get(key)), str(r.get("symbol"))))[:n])
    return out


def cap_ts(rows: Sequence[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out: list[dict[str, Any]] = []
    for _ts, g in sorted(by.items()):
        out.extend(sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("momentum_confirmation_score")), -_num(r.get("entry_trigger_score")), str(r.get("symbol"))))[:cap])
    return out


def cooldown(rows: Sequence[Mapping[str, Any]], minutes: int) -> list[dict[str, Any]]:
    last: dict[str, datetime] = {}
    out: list[dict[str, Any]] = []
    for r in sorted([dict(x) for x in rows], key=lambda x: (x["ts_dt"], -_num(x.get("market_confirmation_score")), str(x.get("symbol")))):
        s = str(r.get("symbol"))
        if s in last and (r["ts_dt"] - last[s]).total_seconds() < minutes * 60:
            continue
        last[s] = r["ts_dt"]
        out.append(r)
    return out


def build_pools() -> tuple[dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]], dict[str, Any]]:
    rows, meta = load_old_radar_rows()
    rows = [dict(r) for r in rows if r.get("return_1h") is not None]
    # Candidate universes use only signal-time fields, not future returns/MFE.
    allu = rows
    core = [r for r in rows if int(r.get("hour_bjt") or 0) in CORE_HOURS]
    night = [r for r in rows if int(r.get("hour_bjt") or 0) in NIGHT_HOURS]
    eligible = [r for r in rows if str(r.get("eligible_long")) == "eligible"]
    watch_hot = [r for r in rows if str(r.get("decision_status")) == "watch_hot"]
    wait_entry = [r for r in rows if str(r.get("recommended_action")) == "wait_for_entry_trigger"]
    day_high = [r for r in rows if str(r.get("session")) == "day_high_threshold"]
    prewarm = [r for r in rows if str(r.get("session")) == "prewarm"]

    specs: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for base_name, univ in [
        ("all", allu), ("core", core), ("night", night),
        ("wait_entry", wait_entry), ("day_high", day_high),
    ]:
        if len(univ) < 80:
            continue
        m10 = top_frac(univ, "market_confirmation_score", 0.10)
        m20 = top_frac(univ, "market_confirmation_score", 0.20)
        # Selected pools are tighter; universe pools are capped for same-ts random controls.
        specs[f"{base_name}_mkt10_cd60"] = (univ, cooldown(m10, 60))
        specs[f"{base_name}_mkt20_cd60"] = (univ, cooldown(m20, 60))

    pools: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    all_for_attach: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    for name, (univ, sel) in specs.items():
        univ_c = cap_ts(univ, 25)
        sel_c = cap_ts(sel, 8)
        pools[name] = (univ_c, sel_c)
        all_for_attach.extend(univ_c); all_for_attach.extend(sel_c)
        counts[name] = {"universe": len(univ_c), "selected": len(sel_c), "symbols": len({str(r.get("symbol")) for r in sel_c}), "timestamps": len({r["ts_dt"] for r in sel_c})}
    attached, raw_meta = s2.attach_raw(all_for_attach)
    by_key = {(r["ts_dt"], str(r.get("symbol"))): r for r in attached}
    attached_pools = {}
    for name, (univ, sel) in pools.items():
        au = [by_key[(r["ts_dt"], str(r.get("symbol")))] for r in univ if (r["ts_dt"], str(r.get("symbol"))) in by_key]
        ase = [by_key[(r["ts_dt"], str(r.get("symbol")))] for r in sel if (r["ts_dt"], str(r.get("symbol"))) in by_key]
        attached_pools[name] = (au, ase)
    return attached_pools, {"old": meta, "counts": counts, "raw_symbol": raw_meta, "cost": "all-taker 8bp", "search_sims": SEARCH_SIMS, "validate_sims": VALIDATE_SIMS}


def bars_from(bars: Sequence[Sequence[Any]], start: datetime, end: datetime) -> list[Sequence[Any]]:
    return s2.v2.base._bars_from(bars, start, end)


def decide_entry(bars: Sequence[Sequence[Any]], signal_dt: datetime, e: Entry) -> tuple[datetime | None, float | None, str]:
    if e.kind == "next5m":
        return s2.v2.decide_entry2(bars, signal_dt, s2.v2.Entry2("N5", "next5m"))
    return s2.v2.decide_entry2(bars, signal_dt, s2.v2.Entry2(e.name, "pullback", pullback=e.pullback, watch_minutes=e.watch_minutes))


def exit_sim(bars: Sequence[Sequence[Any]], entry_dt: datetime, entry_px: float, x: Exit) -> dict[str, Any]:
    if entry_px <= 0:
        return {"pnl": None, "reason": "bad_entry"}
    end = entry_dt + timedelta(minutes=x.time_minutes)
    path = bars_from(bars, entry_dt, end)
    if not path:
        return {"pnl": None, "reason": "no_path"}
    mfe = max(float(b[2]) for b in path) / entry_px - 1.0
    mae = min(float(b[3]) for b in path) / entry_px - 1.0
    sl_px = entry_px * (1 + x.sl)
    if x.kind == "hold":
        for b in path:
            low = float(b[3])
            if low <= sl_px:
                return {"pnl": x.sl - COST, "gross_pnl": x.sl, "reason": "hard_sl", "mfe": mfe, "mae": mae}
        close = float(path[-1][4])
        pnl = close / entry_px - 1.0
        return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "time_exit", "mfe": mfe, "mae": mae}
    if x.kind == "static":
        tp1_px = entry_px * (1 + float(x.tp1 or 0))
        tp2_px = entry_px * (1 + float(x.tp2 or x.tp1 or 0))
        lock_px = entry_px * (1 + x.lock)
        left = 1.0
        pnl = 0.0
        got1 = False
        for b in path:
            high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
            if not got1:
                if low <= sl_px:
                    return {"pnl": x.sl - COST, "gross_pnl": x.sl, "reason": "initial_stop", "mfe": mfe, "mae": mae}
                if high >= tp1_px:
                    pnl += x.size1 * float(x.tp1 or 0)
                    left -= x.size1
                    got1 = True
                    continue
            else:
                if low <= lock_px:
                    pnl += left * x.lock
                    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "lock_after_tp1", "mfe": mfe, "mae": mae}
                if high >= tp2_px:
                    pnl += left * float(x.tp2 or x.tp1 or 0)
                    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp2_after_tp1", "mfe": mfe, "mae": mae}
            if close_ms >= int(end.timestamp() * 1000):
                pnl += left * (close / entry_px - 1.0)
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp1_then_time" if got1 else "time_exit", "mfe": mfe, "mae": mae}
        close = float(path[-1][4])
        pnl += left * (close / entry_px - 1.0)
        return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "tp1_then_time" if got1 else "time_exit", "mfe": mfe, "mae": mae}
    # trail
    act = float(x.activation or 0.0); trail = float(x.trail or 0.0)
    act_px = entry_px * (1 + act)
    left = 1.0; pnl = 0.0; active = False; high_water = entry_px; trail_px = entry_px * (1 + x.lock)
    for b in path:
        high = float(b[2]); low = float(b[3]); close = float(b[4]); close_ms = int(b[6])
        if not active:
            if low <= sl_px:
                return {"pnl": x.sl - COST, "gross_pnl": x.sl, "reason": "initial_stop", "mfe": mfe, "mae": mae}
            if high >= act_px:
                pnl += x.size1 * act
                left -= x.size1
                active = True
                high_water = high
                trail_px = max(entry_px * (1 + x.lock), high_water * (1 - trail))
                continue
        else:
            if low <= trail_px:
                pnl += left * (trail_px / entry_px - 1.0)
                return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_stop", "mfe": mfe, "mae": mae}
            high_water = max(high_water, high)
            trail_px = max(trail_px, high_water * (1 - trail), entry_px * (1 + x.lock))
        if close_ms >= int(end.timestamp() * 1000):
            pnl += left * (close / entry_px - 1.0)
            return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}
    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1.0)
    return {"pnl": pnl - COST, "gross_pnl": pnl, "reason": "trail_time" if active else "time_exit", "mfe": mfe, "mae": mae}


def simulate(rows: Sequence[Mapping[str, Any]], bars_by_symbol: Mapping[str, Sequence[Sequence[Any]]], e: Entry, x: Exit) -> tuple[list[dict[str, Any]], Counter[str]]:
    out = []
    miss = Counter()
    for r0 in rows:
        r = dict(r0)
        bars = bars_by_symbol.get(str(r.get("raw_symbol")), [])
        ed, ep, er = decide_entry(bars, r["ts_dt"], e)
        if ed is None or ep is None:
            miss[er] += 1
            continue
        sim = exit_sim(bars, ed, ep, x)
        if sim.get("pnl") is None:
            miss[str(sim.get("reason"))] += 1
            continue
        r.update(sim); r["entry_dt"] = iso(ed); r["entry_price"] = ep; r["entry_rule"] = e.name; r["exit_rule"] = x.name
        out.append(r)
    return out, miss


def q(xs: Sequence[float], p: float) -> float:
    if not xs: return 0.0
    ys = sorted(xs)
    return ys[min(len(ys)-1, max(0, int(round((len(ys)-1)*p))))]


def random_same_ts(univ: Sequence[Mapping[str, Any]], sel: Sequence[Mapping[str, Any]], seed: int, sims: int) -> dict[str, Any]:
    u_by = defaultdict(list); s_by = defaultdict(list)
    for r in univ: u_by[r["ts_dt"]].append(r)
    for r in sel: s_by[r["ts_dt"]].append(r)
    rng = random.Random(seed)
    avgs=[]; sums=[]; shs=[]
    for _ in range(sims):
        vals=[]
        for ts, sg in s_by.items():
            pool = u_by.get(ts, [])
            if not pool: continue
            n = len(sg)
            sample = pool if n >= len(pool) else rng.sample(pool, n)
            vals.extend(float(r["pnl"]) for r in sample)
        st=stat(vals); avgs.append(st["avg"]); sums.append(st["sum"]); shs.append(st["sharpe_like"])
    return {"avg_p95": q(avgs,.95), "sum_p95": q(sums,.95), "sh_p95": q(shs,.95), "sims": sims}


def cap_port(rows: Sequence[Mapping[str, Any]], cap: int) -> dict[str, float]:
    by = defaultdict(list)
    for r in rows: by[r["ts_dt"]].append(r)
    vals=[]
    for _ts, g in sorted(by.items()):
        vals.extend(float(r["pnl"]) for r in sorted(g, key=lambda r: (-_num(r.get("market_confirmation_score")), -_num(r.get("entry_trigger_score")), str(r.get("symbol"))))[:cap])
    comp=1.0
    for v in vals: comp *= 1+v
    st=stat(vals)
    return {"n": len(vals), "avg": st["avg"], "sum": st["sum"], "sh": st["sharpe_like"], "comp": comp-1, "mdd": s2.v2.base.max_drawdown(vals)}


def summarize(pool: str, e: Entry, x: Exit, selected_n: int, rows: Sequence[Mapping[str, Any]], rand: Mapping[str, Any], miss: Counter[str]) -> dict[str, Any]:
    vals=[float(r["pnl"]) for r in rows]
    st=stat(vals)
    days=defaultdict(list)
    for r in rows: days[bjt_day(r)].append(float(r["pnl"]))
    day_stats={d:{"n":len(v),"sum":sum(v),"avg":sum(v)/len(v) if v else 0.0} for d,v in sorted(days.items())}
    pnl_by_sym=defaultdict(float); n_by_sym=Counter()
    for r in rows:
        s=str(r.get("symbol")); pnl_by_sym[s]+=float(r["pnl"]); n_by_sym[s]+=1
    top=sorted(pnl_by_sym.items(), key=lambda kv: kv[1], reverse=True)[:5]
    rem=[float(r["pnl"]) for r in rows if str(r.get("symbol")) not in {s for s,_ in top}]
    rem_st=stat(rem)
    out={
        "pool":pool,"entry":e.name,"exit":x.name,"n":len(rows),"selected_n":selected_n,
        "avg":st["avg"],"sum":st["sum"],"sh":st["sharpe_like"],"mdd":st["mdd"],
        "rand_avg_p95":rand["avg_p95"],"rand_sum_p95":rand["sum_p95"],"rand_sh_p95":rand["sh_p95"],
        "edge_avg":st["avg"]-rand["avg_p95"],"edge_sum":st["sum"]-rand["sum_p95"],
        "cap5":cap_port(rows,5),"cap10":cap_port(rows,10),"rem_top5_avg":rem_st["avg"],"rem_top5_sum":rem_st["sum"],
        "pos_days":sum(1 for d in day_stats.values() if d["sum"]>0),"days":len(day_stats),"day_stats":day_stats,
        "stop_rate":sum(1 for r in rows if str(r.get("reason")) in {"initial_stop","hard_sl"})/max(1,len(rows)),
        "exec_rate":len(rows)/max(1,selected_n),"reasons":dict(Counter(str(r.get("reason")) for r in rows)),
        "top_symbols":[{"symbol":s,"pnl":v,"n":n_by_sym[s]} for s,v in top],"miss":dict(miss),
    }
    score=0.0
    score += out["edge_avg"]*10000
    score += max(-1, min(1, out["rem_top5_avg"]))*1000
    score += (out["pos_days"]/max(1,out["days"]))*2
    score += min(5, out["sh"])
    score -= max(0, out["stop_rate"]-0.28)*8
    if len(rows)<40: score -= 5
    if out["edge_avg"]<=0: score -= 4
    if out["rem_top5_avg"]<=0: score -= 3
    out["score"] = score
    return out


def run_combo(pool_name: str, univ_rows: Sequence[Mapping[str, Any]], sel_rows: Sequence[Mapping[str, Any]], bars: Mapping[str, Sequence[Sequence[Any]]], e: Entry, x: Exit, sims: int, seed: int) -> dict[str, Any] | None:
    sel_exec, miss = simulate(sel_rows, bars, e, x)
    if len(sel_exec) < 25:
        return None
    univ_exec, _ = simulate(univ_rows, bars, e, x)
    if len(univ_exec) < len(sel_exec):
        return None
    rand = random_same_ts(univ_exec, sel_exec, seed, sims)
    return summarize(pool_name, e, x, len(sel_rows), sel_exec, rand, miss)


def render(meta: dict, top: list[dict], validated: list[dict]) -> str:
    lines=["# Hotcoin execution scheme search", "", f"generated_utc: `{iso(datetime.now(timezone.utc))}`", "", "## Meta", "```text", json.dumps(meta, ensure_ascii=False, indent=2, default=str), "```", "", "## Top search candidates", "```text"]
    for r in top[:30]:
        lines.append(f"score={r['score']:6.2f} {r['pool']:<28} {r['entry']+'__'+r['exit']:<25} n={r['n']:4d}/{r['selected_n']:<4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg']):>8} sum={pct(r['sum']):>9}/{pct(r['rand_sum_p95']):>9} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} cap5={pct(r['cap5']['comp']):>9} remT5={pct(r['rem_top5_avg']):>8} days={r['pos_days']}/{r['days']} stop={r['stop_rate']*100:4.1f}%")
    lines += ["```", "", "## Validated candidates", "```text"]
    for r in validated:
        lines.append(f"{r['pool']:<28} {r['entry']+'__'+r['exit']:<25} n={r['n']:4d}/{r['selected_n']:<4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg']):>8} sum={pct(r['sum']):>9}/{pct(r['rand_sum_p95']):>9} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} cap5={pct(r['cap5']['comp']):>9} cap10={pct(r['cap10']['comp']):>9} remT5={pct(r['rem_top5_avg']):>8}/{pct(r['rem_top5_sum']):>8} days={r['pos_days']}/{r['days']} stop={r['stop_rate']*100:4.1f}% exec={r['exec_rate']*100:4.1f}%")
        lines.append("  reasons="+json.dumps(r['reasons'], ensure_ascii=False)+" top="+", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in r['top_symbols']))
    lines += ["```", "", "## Verdict", "```text"]
    if validated:
        best=validated[0]
        if best['edge_avg']>0 and best['rem_top5_avg']>0 and best['pos_days']>=int(best['days']*0.65):
            lines.append(f"FOUND shadow-grade hotcoin execution scheme: {best['pool']} + {best['entry']} + {best['exit']}. It passes full-window same-ts random p95 on avg, remains positive after top-symbol removal, and has acceptable day positivity. Treat as shadow until fresh-forward confirms.")
        else:
            lines.append("No production-grade full-window hotcoin execution scheme found. Best candidates are diagnostic/shadow only; discovery remains stronger than executable capture.")
    else:
        lines.append("No validated candidates.")
    lines += ["```", ""]
    return "\n".join(lines)


def main() -> int:
    pools, meta = build_pools()
    all_rows=[]
    for univ, sel in pools.values(): all_rows.extend(univ); all_rows.extend(sel)
    bars, bars_meta = s2.v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in EXITS)+90)
    meta["bars"] = bars_meta
    candidates=[]
    seed=2026060401
    combos=0
    for pname, (univ, sel) in sorted(pools.items()):
        if len(sel)<30: continue
        for e in ENTRIES:
            for x in EXITS:
                combos += 1
                r=run_combo(pname, univ, sel, bars, e, x, SEARCH_SIMS, seed+combos*17)
                if r is not None: candidates.append(r)
    candidates.sort(key=lambda r: r["score"], reverse=True)
    validated=[]
    seen=set()
    for r0 in candidates[:12]:
        key=(r0['pool'], r0['entry'], r0['exit'])
        if key in seen: continue
        seen.add(key)
        e=next(x for x in ENTRIES if x.name==r0['entry'])
        x=next(y for y in EXITS if y.name==r0['exit'])
        univ, sel = pools[r0['pool']]
        rr=run_combo(r0['pool'], univ, sel, bars, e, x, VALIDATE_SIMS, seed+9999+len(validated)*101)
        if rr: validated.append(rr)
    validated.sort(key=lambda r: (r['edge_avg']>0, r['rem_top5_avg']>0, r['pos_days']/max(1,r['days']), r['score']), reverse=True)
    result={"generated_utc": iso(datetime.now(timezone.utc)), "meta": meta, "top": candidates[:50], "validated": validated}
    ts=datetime.now().strftime("%Y%m%d-%H%M%S")
    jp=OUT/f"hotcoin-execution-scheme-search-{ts}.json"; mp=OUT/f"hotcoin-execution-scheme-search-{ts}.md"
    lj=OUT/"hotcoin-execution-scheme-search-latest.json"; lm=OUT/"hotcoin-execution-scheme-search-latest.md"
    md=render(meta, candidates, validated)
    for p in [jp, lj]: p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+"\n")
    for p in [mp, lm]: p.write_text(md+"\n")
    print(jp); print(mp); print(lj); print(lm)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
