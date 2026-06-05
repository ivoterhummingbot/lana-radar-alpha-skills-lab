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
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from radar_alpha_skills_lab.old_radar_alpha import load_old_radar_rows, parse_ts  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import _decorate_old_rows, build_old_candidate_sets  # noqa: E402
from radar_alpha_skills_lab.signal_control import (  # noqa: E402
    COSTS,
    fetch_1m_klines,
    fetch_exchange_symbols,
    simulate_managed_1h,
    stat,
    to_fapi_symbol,
    cap_portfolio,
    _stats_for_rows,
)

BJ = timezone(timedelta(hours=8))
COST = COSTS["all_taker_8bp_total"]


def pct(x: float | None) -> str:
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return "na"
    return f"{float(x) * 100:+.2f}%"


def core(rows: Sequence[Mapping[str, Any]], session: str) -> list[dict[str, Any]]:
    out = []
    for r0 in rows:
        r = dict(r0)
        h = int(r.get("hour_bjt") or r["ts_dt"].astimezone(BJ).hour)
        if session == "core_20_08":
            ok = h >= 20 or h < 8
        elif session == "night_20_04":
            ok = h >= 20 or h < 4
        elif session == "dawn_04_08":
            ok = 4 <= h < 8
        else:
            ok = True
        if ok:
            out.append(r)
    return out


def bar_open_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def features_from_bars(raw: str, ts: datetime, bars: Sequence[Sequence[Any]], btc_bars: Sequence[Sequence[Any]]) -> dict[str, Any] | None:
    # Use the last fully closed 5 one-minute bars before signal timestamp.
    ts_ms = int(ts.timestamp() * 1000)
    prev = [b for b in bars if int(b[6]) < ts_ms]
    if len(prev) < 5:
        return None
    last5 = prev[-5:]
    o = float(last5[0][1]); h = max(float(b[2]) for b in last5); l = min(float(b[3]) for b in last5); c = float(last5[-1][4])
    vol = sum(float(b[5]) for b in last5)
    typical_vol = None
    if len(prev) >= 20:
        prior = prev[-20:-5]
        pv = sum(float(b[5]) for b in prior) / max(1, len(prior)) * 5
        typical_vol = pv
    ret5 = c / o - 1 if o > 0 else 0.0
    upper_wick = (h - max(o, c)) / max(1e-12, h - l) if h > l else 0.0
    vwap = sum(float(b[4]) * float(b[5]) for b in last5) / max(1e-12, vol)
    vwap_dist = c / vwap - 1 if vwap > 0 else 0.0
    btc_prev = [b for b in btc_bars if int(b[6]) < ts_ms]
    btc_ret = 0.0
    if len(btc_prev) >= 5:
        b5 = btc_prev[-5:]
        bo = float(b5[0][1]); bc = float(b5[-1][4])
        btc_ret = bc / bo - 1 if bo > 0 else 0.0
    return {
        "prev5m_ret": ret5,
        "prev5m_upper_wick_ratio": upper_wick,
        "prev5m_volume_ratio": (vol / typical_vol if typical_vol and typical_vol > 0 else None),
        "prev5m_vwap_distance": vwap_dist,
        "btc_5m_ret": btc_ret,
        "symbol_rel5_vs_btc": ret5 - btc_ret,
    }


def attach_prev5_and_managed(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {}
    min_dt = min(r["ts_dt"] for r in rows) - timedelta(minutes=25)
    max_dt = max(r["ts_dt"] for r in rows) + timedelta(hours=1, minutes=5)
    syms = sorted({str(r["raw_symbol"]) for r in rows})
    errors: dict[str, str] = {}
    by: dict[str, list[list[Any]]] = {}
    for raw in syms + ["BTCUSDT"]:
        try:
            by[raw] = fetch_1m_klines(raw, min_dt, max_dt)
        except Exception as exc:  # noqa: BLE001
            errors[raw] = str(exc)[:240]
            by[raw] = []
    out = []
    no_feat = no_path = 0
    for r0 in rows:
        r = dict(r0)
        raw = str(r["raw_symbol"])
        feat = features_from_bars(raw, r["ts_dt"], by.get(raw, []), by.get("BTCUSDT", []))
        if feat is None:
            no_feat += 1
            continue
        sim = simulate_managed_1h(by.get(raw, []), r["ts_dt"])
        if sim.get("pnl") is None:
            no_path += 1
            continue
        r.update(feat)
        r["managed_1h"] = float(sim["pnl"])
        r["managed_1h_reason"] = sim.get("reason")
        r["entry_price"] = sim.get("entry_price")
        out.append(r)
    return out, {"rows_in": len(rows), "rows_out": len(out), "no_feat": no_feat, "no_path": no_path, "symbols": len(syms), "errors": errors}


def top_per_ts(rows: Sequence[Mapping[str, Any]], field: str, fraction: float, reverse: bool = True) -> list[dict[str, Any]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    out = []
    for _ts, group in sorted(by.items()):
        n = max(1, math.ceil(len(group) * fraction))
        out.extend(sorted(group, key=lambda r: ((-1 if reverse else 1) * float(r.get(field) or 0.0), str(r.get("symbol"))))[:n])
    return out


def select(rows: Sequence[Mapping[str, Any]], rule: str) -> list[dict[str, Any]]:
    if rule == "all":
        return list(map(dict, rows))
    if rule == "top20_rel5":
        return top_per_ts([r for r in rows if float(r.get("symbol_rel5_vs_btc") or 0.0) >= 0.0], "symbol_rel5_vs_btc", 0.20)
    if rule == "top10_rel5":
        return top_per_ts(rows, "symbol_rel5_vs_btc", 0.10)
    if rule == "top20_prev5ret":
        return top_per_ts([r for r in rows if float(r.get("prev5m_ret") or 0.0) >= 0.0], "prev5m_ret", 0.20)
    if rule == "top10_prev5ret":
        return top_per_ts(rows, "prev5m_ret", 0.10)
    if rule == "strict_rel5_wick":
        base = [r for r in rows if float(r.get("symbol_rel5_vs_btc") or 0.0) >= 0.002 and float(r.get("prev5m_upper_wick_ratio") or 0.0) <= 0.50]
        return top_per_ts(base, "symbol_rel5_vs_btc", 0.33)
    raise KeyError(rule)


def random_dist(universe: Sequence[Mapping[str, Any]], cand: Sequence[Mapping[str, Any]], sims: int, seed: int) -> dict[str, Any]:
    by_uni: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    by_c: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in universe:
        by_uni[r["ts_dt"]].append(dict(r))
    for r in cand:
        by_c[r["ts_dt"]].append(dict(r))
    rng = random.Random(seed)
    avgs=[]; sums=[]
    for _ in range(sims):
        vals=[]
        for ts, group in by_c.items():
            pool=by_uni.get(ts, [])
            if not pool:
                continue
            sample=rng.sample(pool, min(len(group), len(pool)))
            vals.extend(float(r["managed_1h"]) - COST for r in sample)
        st=stat(vals)
        avgs.append(st["avg"]); sums.append(st["sum"])
    def q(xs, p):
        xs=sorted(xs)
        if not xs: return None
        return xs[min(len(xs)-1, int(math.ceil(p*len(xs))-1))]
    return {"avg": {"p95": q(avgs, .95), "p50": q(avgs, .5)}, "sum": {"p95": q(sums, .95), "p50": q(sums, .5)}}


def summarize(rows: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]], sims: int, seed: int) -> dict[str, Any]:
    st = {
        "taker": _stats_for_rows(rows, COST),
        "gross": _stats_for_rows(rows, 0.0),
        "cap5": cap_portfolio(rows, "managed_1h", COST, 5),
        "cap10": cap_portfolio(rows, "managed_1h", COST, 10),
    }
    rd = random_dist(universe, rows, sims, seed)
    bysym=defaultdict(float); cnt=Counter()
    for r in rows:
        bysym[str(r.get("symbol"))]+=float(r["managed_1h"])-COST; cnt[str(r.get("symbol"))]+=1
    ranked=sorted(bysym.items(), key=lambda kv: kv[1], reverse=True)
    rem={s for s,_ in ranked[:5]}
    return {"n": len(rows), "stats": st, "random": rd, "top": [(s,v,cnt[s]) for s,v in ranked[:10]], "bottom": [(s,v,cnt[s]) for s,v in sorted(bysym.items(), key=lambda kv:kv[1])[:8]], "remove_top5": stat(float(r["managed_1h"])-COST for r in rows if str(r.get("symbol")) not in rem)}


def main() -> int:
    sims=200
    old_rows, meta = load_old_radar_rows()
    cutoff = parse_ts(str(meta["complete_cutoffs"]["1h"]))
    ready = _decorate_old_rows(old_rows, cutoff)
    # Recent windows where A prev5m data existed and user is optimizing now.
    windows = [
        ("BJT_2026-05-31", datetime(2026,5,31,16,0,tzinfo=timezone.utc)),
        ("BJT_2026-06-01", datetime(2026,6,1,16,0,tzinfo=timezone.utc)),
        ("BJT_2026-06-02", datetime(2026,6,2,16,0,tzinfo=timezone.utc)),
        ("latest24", cutoff),
    ]
    result={"generated_utc": datetime.now(timezone.utc).isoformat(), "old_meta": meta, "sims": sims, "windows": {}}
    lines=["# Old B/C prev5m OHLC revalidation", "", f"generated_utc: `{result['generated_utc']}`", "", "Method: old radar rows; prev5m features causally recomputed from previous closed 1m bars; managed_1h same as new-radar short execution; all-taker 8bp; random same timestamp.", "", "## Summary", "```text"]
    for wname,end in windows:
        start=end-timedelta(hours=24)
        wrows=[r for r in ready if start <= r["ts_dt"] <= end]
        sets=build_old_candidate_sets(wrows)
        result["windows"][wname]={"end_utc": end.isoformat(), "sets": {}}
        for set_name in ["old_watch_hot", "old_market_top20", "old_core_night_market_top20"]:
            base=sets[set_name]
            base=core(base, "core_20_08") if set_name != "old_core_night_market_top20" else base
            with_feat, attach_meta=attach_prev5_and_managed(base)
            result["windows"][wname]["sets"][set_name]={"attach_meta": attach_meta, "rules": {}}
            lines.append(f"{wname} {set_name} base={len(base)} with_path={len(with_feat)} attach={attach_meta}")
            for ri, rule in enumerate(["all", "top20_rel5", "top10_rel5", "top20_prev5ret", "top10_prev5ret", "strict_rel5_wick"]):
                cand=select(with_feat, rule)
                if len(cand)<15:
                    result["windows"][wname]["sets"][set_name]["rules"][rule]={"n": len(cand), "insufficient": True}
                    lines.append(f"  {rule:<18} insufficient n={len(cand)}")
                    continue
                block=summarize(cand, with_feat, sims, 20260603+ri)
                result["windows"][wname]["sets"][set_name]["rules"][rule]=block
                t=block["stats"]["taker"]
                lines.append(f"  {rule:<18} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} med={pct(t['median']):>8} win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} mdd={pct(t['mdd']):>8} cap5={pct(block['stats']['cap5']['comp']):>8} cap10={pct(block['stats']['cap10']['comp']):>8} rand_p95_avg={pct(block['random']['avg']['p95']):>8} remTop5_avg={pct(block['remove_top5']['avg']):>8}")
            lines.append("")
    lines.append("```")
    lines.append("")
    lines.append("## Top symbols")
    for wname, wb in result["windows"].items():
        lines.append(f"### {wname}")
        for set_name, sb in wb["sets"].items():
            for rule, block in sb["rules"].items():
                if block.get("insufficient"):
                    continue
                top=", ".join(f"{s} {pct(v)}/{n}" for s,v,n in block["top"][:6])
                lines.append(f"- {set_name} {rule}: {top}")
    out=PROJECT_ROOT/"output"/f"old-bc-prev5m-ohcl-revalidation-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+"\n")
    out.with_suffix('.md').write_text("\n".join(lines)+"\n")
    print(out.with_suffix('.json'))
    print(out.with_suffix('.md'))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
