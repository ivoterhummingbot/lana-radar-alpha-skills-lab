#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
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

from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.old_radar_alpha import TOKENIZED_STOCKS, clean_symbol, parse_ts  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import attach_short_horizon_ohlc, candidate_top_fraction_by_ts  # noqa: E402
from radar_alpha_skills_lab.score_regime import bjt_session  # noqa: E402
from radar_alpha_skills_lab.signal_control import fetch_exchange_symbols, iso, load_snapshot_rows, pct, stat, to_fapi_symbol  # noqa: E402

BJ = timezone(timedelta(hours=8))
HORIZONS = ["15m", "30m", "1h"]
SIMS = 200
WINDOWS = ["all", "core_20_08", "night_20_04", "dawn_04_08"]
CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))
DAWN_HOURS = set(range(4, 8))


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def vals(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        x = float(v)
        if math.isfinite(x):
            out.append(x)
    return out


def group_by_ts(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    by: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["ts_dt"]].append(dict(r))
    return by


def random_p95(universe: Sequence[Mapping[str, Any]], cand: Sequence[Mapping[str, Any]], key: str, seed: int) -> dict[str, float]:
    if not cand or len(cand) >= len(universe):
        return {"avg_p95": 0.0, "sum_p95": 0.0}
    u_by = group_by_ts(universe)
    c_counts = {ts: len(g) for ts, g in group_by_ts(cand).items()}
    rng = random.Random(seed)
    avgs, sums = [], []
    for _ in range(SIMS):
        picked = []
        for ts, n in c_counts.items():
            pool = [r for r in u_by.get(ts, []) if r.get(key) is not None]
            if not pool:
                continue
            pool = sorted(pool, key=lambda r: str(r.get("symbol") or ""))
            picked.extend(pool if n >= len(pool) else rng.sample(pool, n))
        st = stat(float(r[key]) for r in picked if r.get(key) is not None)
        avgs.append(st["avg"])
        sums.append(st["sum"])
    def q95(xs: list[float]) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        return xs[min(len(xs)-1, max(0, int(round((len(xs)-1)*0.95))))]
    return {"avg_p95": q95(avgs), "sum_p95": q95(sums)}


def top_remove(rows: Sequence[Mapping[str, Any]], key: str, n: int = 5) -> dict[str, Any]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[str(r.get("symbol"))].append(float(r[key]))
    ranked = sorted(((s, sum(v), len(v)) for s, v in by.items()), key=lambda x: x[1], reverse=True)
    rem = {s for s, _v, _n in ranked[:n]}
    return {"top": ranked[:10], "remove_top5": stat(float(r[key]) for r in rows if r.get(key) is not None and str(r.get("symbol")) not in rem)}


def day_stat(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[str(r.get("date_bjt"))].append(float(r[key]))
    avgs = {d: sum(v)/len(v) for d, v in sorted(by.items()) if v}
    return {"days": len(avgs), "positive_avg_days": sum(1 for v in avgs.values() if v > 0), "avg_by_day": avgs, "n_by_day": {d: len(v) for d, v in sorted(by.items())}}


def summarize(name: str, window: str, universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    block = {"name": name, "window": window, "n": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "h": {}}
    for i, h in enumerate(HORIZONS):
        rk, mk = f"ret_{h}", f"mfe_{h}"
        rs, ms = stat(vals(rows, rk)), stat(vals(rows, mk))
        rr, mr = random_p95(universe, rows, rk, seed + i*100), random_p95(universe, rows, mk, seed + i*100 + 17)
        mfev = vals(rows, mk)
        block["h"][h] = {
            "close": rs,
            "mfe": ms,
            "rand_close_p95": rr,
            "rand_mfe_p95": mr,
            "close_pass": rs["avg"] > rr["avg_p95"] if rows and len(rows) < len(universe) else None,
            "mfe_pass": ms["avg"] > mr["avg_p95"] if rows and len(rows) < len(universe) else None,
            "hit1": (sum(1 for v in mfev if v >= 0.01) / len(mfev)) if mfev else 0.0,
            "hit2": (sum(1 for v in mfev if v >= 0.02) / len(mfev)) if mfev else 0.0,
            "top_remove_close": top_remove(rows, rk),
            "day_close": day_stat(rows, rk),
        }
    return block


def filter_win(rows: Sequence[Mapping[str, Any]], window: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        h = int(r.get("hour_bjt") or 0)
        ok = window == "all" or (window == "core_20_08" and h in CORE_HOURS) or (window == "night_20_04" and h in NIGHT_HOURS) or (window == "dawn_04_08" and h in DAWN_HOURS)
        if ok:
            out.append(dict(r))
    return out


def load_old_signal_scores() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tradable = fetch_exchange_symbols()
    q = """
        select ts, symbol, community_heat_score, market_confirmation_score,
               momentum_confirmation_score, momentum_stage, entry_trigger_score,
               entry_trigger_stage, episode_quality_score, regime_score, final_score,
               decision_status, recommended_action
        from lana_community_scores
        order by ts, symbol, decision_status
    """
    rows = []
    raw = invalid = tokenized = 0
    with sqlite3.connect(f"file:{DEFAULT_SOURCE.community_history_db}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for r0 in con.execute(q):
            raw += 1
            r = dict(r0)
            sym = clean_symbol(str(r.get("symbol") or ""))
            if not sym:
                invalid += 1
                continue
            if sym in TOKENIZED_STOCKS:
                tokenized += 1
                continue
            fapi = to_fapi_symbol(sym, tradable)
            if fapi is None:
                invalid += 1
                continue
            ts = parse_ts(str(r["ts"]))
            bjt = ts.astimezone(BJ)
            row = {"ts": iso(ts), "ts_dt": ts, "ts_bjt": bjt.strftime("%Y-%m-%d %H:%M"), "date_bjt": bjt.strftime("%Y-%m-%d"), "hour_bjt": bjt.hour, "symbol": sym, "raw_symbol": fapi, "session": bjt_session(bjt.hour), "decision_status": str(r.get("decision_status") or ""), "recommended_action": str(r.get("recommended_action") or "")}
            for f in ["community_heat_score", "market_confirmation_score", "momentum_confirmation_score", "entry_trigger_score", "episode_quality_score", "regime_score", "final_score"]:
                row[f] = _num(r.get(f))
            rows.append(row)
    return rows, {"table": "lana_community_scores_only", "raw_rows": raw, "tradable_rows": len(rows), "invalid": invalid, "tokenized_removed": tokenized, "min_ts": min((r["ts"] for r in rows), default=None), "max_ts": max((r["ts"] for r in rows), default=None)}


def fmt(block: Mapping[str, Any]) -> str:
    flags = []
    for h in HORIZONS:
        hb = block["h"][h]
        flags.append(("C" if hb["close_pass"] else "-") + ("M" if hb["mfe_pass"] else "-"))
    h1 = block["h"]["1h"]
    c, m = h1["close"], h1["mfe"]
    return f"{block['name']:<28} {block['window']:<12} n={block['n']:5d} sym={block['symbols']:3d} flags={'/'.join(flags):<8} 1h_ret={pct(c['avg']):>8}/{pct(h1['rand_close_p95']['avg_p95']):>8} 1h_mfe={pct(m['avg']):>8}/{pct(h1['rand_mfe_p95']['avg_p95']):>8} hit1={h1['hit1']*100:5.1f}% hit2={h1['hit2']*100:5.1f}% days={h1['day_close']['positive_avg_days']}/{h1['day_close']['days']} remT5avg={pct(h1['top_remove_close']['remove_top5']['avg']):>8}"


def main() -> int:
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    new_path, new_path_meta = attach_short_horizon_ohlc(new_rows)
    new_path = [r for r in new_path if r.get("ret_1h") is not None]

    old_rows, old_meta = load_old_signal_scores()
    old_path, old_path_meta = attach_short_horizon_ohlc(old_rows)
    old_path = [r for r in old_path if r.get("ret_1h") is not None]

    A = candidate_top_fraction_by_ts(new_path, "market_confirmation_score", 0.20)
    B = [dict(r) for r in old_path if str(r.get("decision_status")) == "watch_hot"]
    C = candidate_top_fraction_by_ts([dict(r) for r in old_path if str(r.get("session")) == "core_night"], "market_confirmation_score", 0.20)

    b_exact = {(r["ts_dt"], str(r.get("symbol"))) for r in B}
    AB_exact = [dict(r) for r in A if (r["ts_dt"], str(r.get("symbol"))) in b_exact]
    b_by_sym: defaultdict[str, list[datetime]] = defaultdict(list)
    for r in B:
        b_by_sym[str(r.get("symbol"))].append(r["ts_dt"])
    AB_near30 = [dict(r) for r in A if any(abs((r["ts_dt"] - t).total_seconds()) <= 1800 for t in b_by_sym.get(str(r.get("symbol")), []))]

    defs = {
        "A_new_market_top20": (new_path, A),
        "B_old_watch_hot": (old_path, B),
        "AB_exact": (A, AB_exact),
        "AB_near30_DIAG": (A, AB_near30),
        "C_old_core_top20": (old_path, C),
    }
    result = {"generated_utc": iso(datetime.now(timezone.utc)), "method": "FIRST-LAYER DISCOVERY ONLY; signals from score tables only; old radar does not use community_forward_outcomes; labels recomputed from OHLC after signal using next complete 15m open; no execution/managed_1h.", "sims": SIMS, "meta": {"new": new_meta, "new_path": new_path_meta, "old": old_meta, "old_path": old_path_meta, "counts": {"A": len(A), "B": len(B), "AB_exact": len(AB_exact), "AB_near30": len(AB_near30), "C": len(C)}}, "sets": {}}
    lines = ["# Full-data first-layer discovery validation (fast clean)", "", f"generated_utc: `{result['generated_utc']}`", "", result["method"], "", "## Coverage", "```text", f"new_path_rows={len(new_path)} latest_snapshot={new_meta.get('latest_snapshot_utc')} complete_end={new_meta.get('complete_end_utc')}", f"old_path_rows={len(old_path)} old_min={old_meta.get('min_ts')} old_max={old_meta.get('max_ts')} old_table={old_meta.get('table')}", f"counts A={len(A)} B={len(B)} AB_exact={len(AB_exact)} AB_near30={len(AB_near30)} C={len(C)}", "```", "", "## Summary", "", "flags: 15m/30m/1h, C=close avg beats same-ts random p95, M=MFE avg beats same-ts random p95.", "", "```text"]
    idx = 0
    for name, (univ0, rows0) in defs.items():
        result["sets"][name] = {}
        for w in WINDOWS:
            univ = filter_win(univ0, w)
            rows = filter_win(rows0, w)
            block = summarize(name, w, univ, rows, 2026060300 + idx * 1000)
            result["sets"][name][w] = block
            lines.append(fmt(block))
            idx += 1
    lines.extend(["```", "", "## 1h day-level notes", "```text"])
    for name in ["A_new_market_top20", "AB_exact", "AB_near30_DIAG", "B_old_watch_hot", "C_old_core_top20"]:
        for w in ["all", "core_20_08", "night_20_04", "dawn_04_08"]:
            b = result["sets"][name][w]
            h = b["h"]["1h"]
            day = h["day_close"]
            lines.append(f"{name:<28} {w:<12} days={day['positive_avg_days']}/{day['days']} avg_by_day=" + ", ".join(f"{d}:{pct(v)}" for d, v in day["avg_by_day"].items()))
    lines.append("```")

    out = PROJECT_ROOT / "output" / f"full-data-first-layer-discovery-fast-clean-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
