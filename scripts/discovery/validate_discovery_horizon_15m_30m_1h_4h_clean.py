#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import validate_full_data_first_layer_discovery_fast_clean as base  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.old_radar_replay import ceil_next_interval, fetch_15m_klines  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import candidate_top_fraction_by_ts  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, load_snapshot_rows, pct  # noqa: E402

BJ = timezone(timedelta(hours=8))
HORIZON_BARS = {"15m": 1, "30m": 2, "1h": 4, "4h": 16}
HORIZONS = list(HORIZON_BARS.keys())
SIMS = 200
WINDOWS = ["all", "core_20_08", "night_20_04", "dawn_04_08"]

# Reuse formatting/window helpers from the previous clean script, but extend horizon list.
base.HORIZONS = HORIZONS
base.SIMS = SIMS


def _values(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        x = float(v)
        if math.isfinite(x):
            out.append(x)
    return out


def _group_values_by_ts(rows: Sequence[Mapping[str, Any]], key: str) -> dict[datetime, list[float]]:
    by: defaultdict[datetime, list[float]] = defaultdict(list)
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        x = float(v)
        if math.isfinite(x):
            by[r["ts_dt"]].append(x)
    return {ts: xs for ts, xs in by.items() if xs}


def fast_random_p95(universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], key: str, seed: int) -> dict[str, float]:
    if not rows or len(rows) >= len(universe):
        return {"avg_p95": 0.0, "sum_p95": 0.0}
    pool_by_ts = _group_values_by_ts(universe, key)
    counts: defaultdict[datetime, int] = defaultdict(int)
    for r in rows:
        if r.get(key) is not None:
            counts[r["ts_dt"]] += 1
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    for _ in range(SIMS):
        total = 0.0
        n_total = 0
        for ts, n in counts.items():
            pool = pool_by_ts.get(ts)
            if not pool:
                continue
            if n >= len(pool):
                total += sum(pool)
                n_total += len(pool)
            else:
                sample = rng.sample(pool, n)
                total += sum(sample)
                n_total += n
        sums.append(total)
        avgs.append(total / n_total if n_total else 0.0)

    def q95(xs: list[float]) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        return xs[min(len(xs) - 1, max(0, int(round((len(xs) - 1) * 0.95))))]

    return {"avg_p95": q95(avgs), "sum_p95": q95(sums)}


def fast_summarize(name: str, window: str, universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], seed: int) -> dict[str, Any]:
    block: dict[str, Any] = {"name": name, "window": window, "n": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "h": {}}
    for i, h in enumerate(HORIZONS):
        rk, mk = f"ret_{h}", f"mfe_{h}"
        rs, ms = base.stat(_values(rows, rk)), base.stat(_values(rows, mk))
        rr, mr = fast_random_p95(universe, rows, rk, seed + i * 100), fast_random_p95(universe, rows, mk, seed + i * 100 + 17)
        mfev = _values(rows, mk)
        block["h"][h] = {
            "close": rs,
            "mfe": ms,
            "rand_close_p95": rr,
            "rand_mfe_p95": mr,
            "close_pass": rs["avg"] > rr["avg_p95"] if rows and len(rows) < len(universe) else None,
            "mfe_pass": ms["avg"] > mr["avg_p95"] if rows and len(rows) < len(universe) else None,
            "hit1": (sum(1 for v in mfev if v >= 0.01) / len(mfev)) if mfev else 0.0,
            "hit2": (sum(1 for v in mfev if v >= 0.02) / len(mfev)) if mfev else 0.0,
            "top_remove_close": base.top_remove(rows, rk),
            "day_close": base.day_stat(rows, rk),
        }
    return block


def _bar_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def simulate_horizons_4h(bars: Sequence[Sequence[Any]], signal_dt: datetime) -> dict[str, Any]:
    entry_dt = ceil_next_interval(signal_dt, minutes=15)
    path = [b for b in bars if int(b[0]) >= int(entry_dt.timestamp() * 1000)]
    if not path:
        return {"entry_dt": None, "entry_price": None, "reason": "no_entry"}
    entry = float(path[0][1])
    if entry <= 0:
        return {"entry_dt": iso(_bar_dt(path[0])), "entry_price": entry, "reason": "bad_entry"}
    out: dict[str, Any] = {"entry_dt": iso(_bar_dt(path[0])), "entry_price": entry, "reason": "ok"}
    for name, n_bars in HORIZON_BARS.items():
        sub = path[:n_bars]
        if len(sub) < n_bars:
            out[f"ret_{name}"] = None
            out[f"mfe_{name}"] = None
            out[f"mae_{name}"] = None
            continue
        out[f"ret_{name}"] = float(sub[-1][4]) / entry - 1.0
        out[f"mfe_{name}"] = max(float(b[2]) for b in sub) / entry - 1.0
        out[f"mae_{name}"] = min(float(b[3]) for b in sub) / entry - 1.0
    return out


def attach_ohlc_4h(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"input_rows": 0, "path_rows": 0, "symbols_requested": 0, "symbols_with_errors": {}}
    fetch_start = min(r["ts_dt"] for r in rows) - timedelta(minutes=30)
    fetch_end = max(r["ts_dt"] for r in rows) + timedelta(hours=5)
    symbols = sorted({str(r["raw_symbol"]) for r in rows if r.get("raw_symbol")})
    by_symbol: dict[str, list[list[Any]]] = {}
    errors: dict[str, str] = {}
    for raw in symbols:
        try:
            by_symbol[raw] = fetch_15m_klines(raw, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001
            errors[raw] = str(exc)[:240]
            by_symbol[raw] = []
    out: list[dict[str, Any]] = []
    no_4h = 0
    for row0 in rows:
        row = dict(row0)
        sim = simulate_horizons_4h(by_symbol.get(str(row.get("raw_symbol")), []), row["ts_dt"])
        row.update(sim)
        if row.get("ret_4h") is None:
            no_4h += 1
            continue
        out.append(row)
    return out, {
        "input_rows": len(rows),
        "path_rows": len(out),
        "no_4h_rows": no_4h,
        "symbols_requested": len(symbols),
        "symbols_with_errors": errors,
        "fetch_start_utc": iso(fetch_start),
        "fetch_end_utc": iso(fetch_end),
        "entry_model": "next complete 15m Binance USDT-M open; close return and intrawindow MFE/MAE for 15m/30m/1h/4h; rows require complete 4h path",
    }


def horizon_line(block: Mapping[str, Any]) -> str:
    parts = []
    for h in HORIZONS:
        hb = block["h"][h]
        c, m = hb["close"], hb["mfe"]
        parts.append(
            f"{h}:ret {pct(c['avg'])}/{pct(hb['rand_close_p95']['avg_p95'])} {'C' if hb['close_pass'] else '-'} "
            f"mfe {pct(m['avg'])}/{pct(hb['rand_mfe_p95']['avg_p95'])} {'M' if hb['mfe_pass'] else '-'}"
        )
    return " | ".join(parts)


def summary_line(name: str, window: str, block: Mapping[str, Any]) -> str:
    flags = []
    for h in HORIZONS:
        hb = block["h"][h]
        flags.append(("C" if hb["close_pass"] else "-") + ("M" if hb["mfe_pass"] else "-"))
    h4 = block["h"]["4h"]
    return (
        f"{name:<24} {window:<12} n={block['n']:5d} sym={block['symbols']:3d} flags={'/'.join(flags):<11} "
        f"4h_ret={pct(h4['close']['avg']):>8}/{pct(h4['rand_close_p95']['avg_p95']):>8} "
        f"4h_mfe={pct(h4['mfe']['avg']):>8}/{pct(h4['rand_mfe_p95']['avg_p95']):>8} "
        f"hit1={h4['hit1']*100:5.1f}% hit2={h4['hit2']*100:5.1f}% "
        f"days={h4['day_close']['positive_avg_days']}/{h4['day_close']['days']} remT5avg={pct(h4['top_remove_close']['remove_top5']['avg']):>8}"
    )


def main() -> int:
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    new_path, new_path_meta = attach_ohlc_4h(new_rows)

    old_rows, old_meta = base.load_old_signal_scores()
    old_path, old_path_meta = attach_ohlc_4h(old_rows)

    A = candidate_top_fraction_by_ts(new_path, "market_confirmation_score", 0.20)
    B = [dict(r) for r in old_path if str(r.get("decision_status")) == "watch_hot"]
    C = candidate_top_fraction_by_ts([dict(r) for r in old_path if str(r.get("session")) == "core_night"], "market_confirmation_score", 0.20)

    b_exact = {(r["ts_dt"], str(r.get("symbol"))) for r in B}
    AB_exact = [dict(r) for r in A if (r["ts_dt"], str(r.get("symbol"))) in b_exact]
    b_by_sym: dict[str, list[datetime]] = {}
    for r in B:
        b_by_sym.setdefault(str(r.get("symbol")), []).append(r["ts_dt"])
    AB_near30 = [dict(r) for r in A if any(abs((r["ts_dt"] - t).total_seconds()) <= 1800 for t in b_by_sym.get(str(r.get("symbol")), []))]

    defs = {
        "A_new_market_top20": (new_path, A),
        "B_old_watch_hot": (old_path, B),
        "AB_exact": (A, AB_exact),
        "AB_near30_DIAG": (A, AB_near30),
        "C_old_core_top20": (old_path, C),
    }

    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "FIRST-LAYER DISCOVERY ONLY; score/snapshot signals only; old radar does not use community_forward_outcomes; labels recomputed from post-signal 15m OHLC via next complete 15m open; horizons 15m/30m/1h/4h; same 4h-complete sample for all horizons; no managed_1h/execution.",
        "sims": SIMS,
        "meta": {
            "new": new_meta,
            "new_path": new_path_meta,
            "old": old_meta,
            "old_path": old_path_meta,
            "counts": {"A": len(A), "B": len(B), "AB_exact": len(AB_exact), "AB_near30": len(AB_near30), "C": len(C)},
        },
        "sets": {},
    }
    lines = [
        "# Discovery horizon analysis: 15m / 30m / 1h / 4h",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        result["method"],
        "",
        "## Coverage",
        "```text",
        f"new_path_rows={len(new_path)} latest_snapshot={new_meta.get('latest_snapshot_utc')} complete_end={new_meta.get('complete_end_utc')}",
        f"old_path_rows={len(old_path)} old_min={old_meta.get('min_ts')} old_max={old_meta.get('max_ts')} old_table={old_meta.get('table')}",
        f"counts A={len(A)} B={len(B)} AB_exact={len(AB_exact)} AB_near30={len(AB_near30)} C={len(C)}",
        "```",
        "",
        "## 4h-complete sample summary",
        "",
        "flags: 15m/30m/1h/4h, C=close avg beats same-ts random p95, M=MFE avg beats same-ts random p95.",
        "",
        "```text",
    ]
    idx = 0
    for name, (univ0, rows0) in defs.items():
        result["sets"][name] = {}
        for w in WINDOWS:
            univ = base.filter_win(univ0, w)
            rows = base.filter_win(rows0, w)
            block = fast_summarize(name, w, univ, rows, 2026060400 + idx * 1000)
            result["sets"][name][w] = block
            lines.append(summary_line(name, w, block))
            idx += 1
    lines.extend(["```", "", "## Horizon curves", ""])
    for name in ["A_new_market_top20", "B_old_watch_hot", "AB_near30_DIAG", "C_old_core_top20"]:
        for w in WINDOWS:
            block = result["sets"][name][w]
            lines.append(f"### {name} / {w}")
            lines.append("```text")
            lines.append(f"n={block['n']} symbols={block['symbols']}")
            lines.append(horizon_line(block))
            h4 = block["h"]["4h"]
            day = h4["day_close"]
            lines.append("4h_day_avg=" + ", ".join(f"{d}:{pct(v)} n={day['n_by_day'][d]}" for d, v in day["avg_by_day"].items()))
            top = h4["top_remove_close"]["top"][:8]
            lines.append("4h_top_close=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in top))
            lines.append(f"4h_remove_top5_avg={pct(h4['top_remove_close']['remove_top5']['avg'])} sum={pct(h4['top_remove_close']['remove_top5']['sum'])}")
            lines.append("```")
            lines.append("")

    out = PROJECT_ROOT / "output" / f"discovery-horizon-15m-30m-1h-4h-clean-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
