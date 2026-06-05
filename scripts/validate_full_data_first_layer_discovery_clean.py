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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.old_radar_alpha import TOKENIZED_STOCKS, clean_symbol, parse_ts  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import (  # noqa: E402
    attach_short_horizon_ohlc,
    candidate_top_fraction_by_ts,
    random_same_timestamp_distribution,
)
from radar_alpha_skills_lab.score_regime import bjt_session  # noqa: E402
from radar_alpha_skills_lab.signal_control import fetch_exchange_symbols, iso, load_snapshot_rows, pct, stat, to_fapi_symbol  # noqa: E402

BJ = timezone(timedelta(hours=8))
HORIZONS = ["15m", "30m", "1h"]
SIMS = 500
CORE_HOURS = set(list(range(20, 24)) + list(range(0, 8)))
NIGHT_HOURS = set(list(range(20, 24)) + list(range(0, 4)))
DAWN_HOURS = set(range(4, 8))


def _num(v: Any) -> float:
    try:
        x = float(v or 0.0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def values(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None and math.isfinite(float(r[key]))]


def top_symbol(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[str(r.get("symbol"))].append(float(r[key]))
    ranked = sorted(((s, sum(v), len(v)) for s, v in by.items()), key=lambda x: x[1], reverse=True)
    out: dict[str, Any] = {
        "top": ranked[:12],
        "bottom": sorted(((s, sum(v), len(v)) for s, v in by.items()), key=lambda x: x[1])[:10],
    }
    for n in [1, 3, 5, 10]:
        removed = {s for s, _v, _n in ranked[:n]}
        out[f"remove_top{n}"] = stat(float(r[key]) for r in rows if r.get(key) is not None and str(r.get("symbol")) not in removed)
    return out


def day_split(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[str(r.get("date_bjt"))].append(float(r[key]))
    sums = {d: sum(v) for d, v in sorted(by.items())}
    avgs = {d: (sum(v) / len(v) if v else 0.0) for d, v in sorted(by.items())}
    return {
        "days": len(sums),
        "positive_sum_days": sum(1 for v in sums.values() if v > 0),
        "positive_avg_days": sum(1 for v in avgs.values() if v > 0),
        "sum_by_day": sums,
        "avg_by_day": avgs,
        "n_by_day": {d: len(v) for d, v in sorted(by.items())},
    }


def hour_split(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[int(r.get("hour_bjt") or 0)].append(float(r[key]))
    return {str(h): stat(v) for h, v in sorted(by.items())}


def summarize(label: str, universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], *, seed: int) -> dict[str, Any]:
    block: dict[str, Any] = {"label": label, "rows": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "horizons": {}}
    for i, h in enumerate(HORIZONS):
        ret_key = f"ret_{h}"
        mfe_key = f"mfe_{h}"
        ret_stat = stat(values(rows, ret_key))
        mfe_stat = stat(values(rows, mfe_key))
        rand_ret = random_same_timestamp_distribution(universe, rows, ret_key, sims=SIMS, seed=seed + i * 101) if rows and len(rows) < len(universe) else {}
        rand_mfe = random_same_timestamp_distribution(universe, rows, mfe_key, sims=SIMS, seed=seed + 17 + i * 101) if rows and len(rows) < len(universe) else {}
        ret_p95 = rand_ret.get("avg", {}).get("p95")
        mfe_p95 = rand_mfe.get("avg", {}).get("p95")
        mfe_vals = values(rows, mfe_key)
        block["horizons"][h] = {
            "close_return": ret_stat,
            "mfe": mfe_stat,
            "mfe_hit_1pct": (sum(1 for v in mfe_vals if v >= 0.01) / len(mfe_vals)) if mfe_vals else 0.0,
            "mfe_hit_2pct": (sum(1 for v in mfe_vals if v >= 0.02) / len(mfe_vals)) if mfe_vals else 0.0,
            "random_same_ts_close": rand_ret,
            "random_same_ts_mfe": rand_mfe,
            "close_pass_p95": (ret_stat["avg"] > ret_p95) if ret_p95 is not None else None,
            "mfe_pass_p95": (mfe_stat["avg"] > mfe_p95) if mfe_p95 is not None else None,
            "top_symbol_close": top_symbol(rows, ret_key),
            "top_symbol_mfe": top_symbol(rows, mfe_key),
            "day_close": day_split(rows, ret_key),
            "hour_close": hour_split(rows, ret_key),
        }
    return block


def filter_window(rows: Sequence[Mapping[str, Any]], window: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        h = int(r.get("hour_bjt") or 0)
        if window == "all":
            ok = True
        elif window == "core_20_08":
            ok = h in CORE_HOURS
        elif window == "night_20_04":
            ok = h in NIGHT_HOURS
        elif window == "dawn_04_08":
            ok = h in DAWN_HOURS
        elif window == "noncore":
            ok = h not in CORE_HOURS
        else:
            raise ValueError(window)
        if ok:
            out.append(dict(r))
    return out


def load_old_signal_score_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load old radar signal scores only; no community_forward_outcomes table is used here."""
    tradable = fetch_exchange_symbols()
    query = """
        select ts, symbol, community_heat_score, market_confirmation_score,
               momentum_confirmation_score, momentum_stage,
               entry_trigger_score, entry_trigger_stage, entry_reject_reason,
               episode_quality_score, regime_score, final_score,
               decision_status, reject_reason, recommended_action, created_at
        from lana_community_scores
        order by ts, symbol, decision_status
    """
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    invalid = 0
    tokenized = 0
    with sqlite3.connect(f"file:{DEFAULT_SOURCE.community_history_db}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for r0 in con.execute(query):
            raw_rows += 1
            r = dict(r0)
            sym = clean_symbol(str(r.get("symbol") or ""))
            if not sym:
                invalid += 1
                continue
            if sym in TOKENIZED_STOCKS:
                tokenized += 1
                continue
            raw = to_fapi_symbol(sym, tradable)
            if raw is None:
                invalid += 1
                continue
            ts_dt = parse_ts(str(r["ts"]))
            bjt = ts_dt.astimezone(BJ)
            row = {
                "ts": iso(ts_dt),
                "ts_dt": ts_dt,
                "ts_bjt": bjt.strftime("%Y-%m-%d %H:%M"),
                "date_bjt": bjt.strftime("%Y-%m-%d"),
                "hour_bjt": bjt.hour,
                "symbol": sym,
                "raw_symbol": raw,
                "family": str(r.get("decision_status") or ""),
                "session": bjt_session(bjt.hour),
                "decision_status": str(r.get("decision_status") or ""),
                "recommended_action": str(r.get("recommended_action") or ""),
                "momentum_stage": str(r.get("momentum_stage") or ""),
                "entry_trigger_stage": str(r.get("entry_trigger_stage") or ""),
                "eligible_long": "eligible" if str(r.get("decision_status") or "") in {"watch_hot", "setup_candidate"} and not str(r.get("recommended_action") or "").startswith("avoid") else "not_eligible",
            }
            for f in ["community_heat_score", "market_confirmation_score", "momentum_confirmation_score", "entry_trigger_score", "episode_quality_score", "regime_score", "final_score"]:
                row[f] = _num(r.get(f))
            rows.append(row)
    meta = {
        "source_db": str(DEFAULT_SOURCE.community_history_db),
        "source_table": "lana_community_scores_only_no_forward_outcomes",
        "raw_rows": raw_rows,
        "tradable_signal_rows": len(rows),
        "invalid_symbol_rows": invalid,
        "tokenized_stock_rows_removed": tokenized,
        "min_ts": min((r["ts"] for r in rows), default=None),
        "max_ts": max((r["ts"] for r in rows), default=None),
        "symbols": len({r["symbol"] for r in rows}),
    }
    return rows, meta


def line(name: str, window: str, block: Mapping[str, Any]) -> str:
    flags = []
    for h in HORIZONS:
        hb = block["horizons"][h]
        flags.append(("C" if hb["close_pass_p95"] else "-") + ("M" if hb["mfe_pass_p95"] else "-"))
    h1 = block["horizons"]["1h"]
    st = h1["close_return"]
    mfe = h1["mfe"]
    rp = h1["random_same_ts_close"].get("avg", {}).get("p95")
    mp = h1["random_same_ts_mfe"].get("avg", {}).get("p95")
    return (
        f"{name:<30} {window:<12} n={block['rows']:5d} sym={block['symbols']:3d} flags={'/'.join(flags):<8} "
        f"1h_ret={pct(st['avg']):>8}/{pct(rp) if rp is not None else 'n/a':>8} "
        f"1h_mfe={pct(mfe['avg']):>8}/{pct(mp) if mp is not None else 'n/a':>8} "
        f"hit1={h1['mfe_hit_1pct']*100:5.1f}% hit2={h1['mfe_hit_2pct']*100:5.1f}%"
    )


def detail_lines(name: str, window: str, block: Mapping[str, Any]) -> list[str]:
    out = [f"### {name} / {window}", "", "```text", f"rows={block['rows']} symbols={block['symbols']}"]
    for h in HORIZONS:
        hb = block["horizons"][h]
        st = hb["close_return"]
        mfe = hb["mfe"]
        rp = hb["random_same_ts_close"].get("avg", {}).get("p95")
        mp = hb["random_same_ts_mfe"].get("avg", {}).get("p95")
        day = hb["day_close"]
        out.append(
            f"{h}: close avg={pct(st['avg'])} med={pct(st['median'])} win={st['win']*100:.1f}% sh={st['sharpe_like']:+.2f} sum={pct(st['sum'])} "
            f"| mfe avg={pct(mfe['avg'])} med={pct(mfe['median'])} hit1={hb['mfe_hit_1pct']*100:.1f}% hit2={hb['mfe_hit_2pct']*100:.1f}% "
            f"| rand_close_p95={pct(rp) if rp is not None else 'n/a'} pass={hb['close_pass_p95']} "
            f"rand_mfe_p95={pct(mp) if mp is not None else 'n/a'} mfe_pass={hb['mfe_pass_p95']} "
            f"| pos_days={day['positive_avg_days']}/{day['days']}"
        )
    h1 = block["horizons"]["1h"]
    out.append("top_1h_close=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in h1["top_symbol_close"]["top"][:10]))
    for rem in ["remove_top1", "remove_top3", "remove_top5", "remove_top10"]:
        st = h1["top_symbol_close"][rem]
        out.append(f"1h_close_{rem}: n={st['n']} avg={pct(st['avg'])} sum={pct(st['sum'])} sh={st['sharpe_like']:+.2f}")
    day = h1["day_close"]
    out.append("1h_close_day_avg=" + ", ".join(f"{d}:{pct(v)} n={day['n_by_day'][d]}" for d, v in day["avg_by_day"].items()))
    out.extend(["```", ""])
    return out


def main() -> int:
    # New radar: score snapshots only; load_snapshot_rows selects maker_attn_symbol_scores and does not read outcome labels.
    new_score_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    new_path_rows, new_path_meta = attach_short_horizon_ohlc(new_score_rows)
    new_path_rows = [r for r in new_path_rows if r.get("ret_1h") is not None]

    # Old radar: raw score rows only; labels are recomputed from OHLC, not loaded from community_forward_outcomes.
    old_score_rows, old_meta = load_old_signal_score_rows()
    old_path_rows, old_path_meta = attach_short_horizon_ohlc(old_score_rows)
    old_path_rows = [r for r in old_path_rows if r.get("ret_1h") is not None]

    # Candidate sets are fixed definitions, not searched.
    A = candidate_top_fraction_by_ts(new_path_rows, "market_confirmation_score", 0.20)
    B = [dict(r) for r in old_path_rows if str(r.get("decision_status")) == "watch_hot"]
    C = candidate_top_fraction_by_ts([dict(r) for r in old_path_rows if str(r.get("session")) == "core_night"], "market_confirmation_score", 0.20)

    b_exact = {(r["ts_dt"], str(r.get("symbol"))) for r in B}
    AB_exact = [dict(r) for r in A if (r["ts_dt"], str(r.get("symbol"))) in b_exact]
    b_by_sym: defaultdict[str, list[datetime]] = defaultdict(list)
    for r in B:
        b_by_sym[str(r.get("symbol"))].append(r["ts_dt"])
    AB_near30 = []
    for r in A:
        ts = r["ts_dt"]
        if any(abs((ts - t).total_seconds()) <= 30 * 60 for t in b_by_sym.get(str(r.get("symbol")), [])):
            rr = dict(r)
            rr["ab_confirm_mode"] = "old_watch_hot_same_symbol_within_30m"
            AB_near30.append(rr)

    set_defs: dict[str, tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = {
        "A_new_market_top20": ("new", new_path_rows, A),
        "B_old_watch_hot": ("old", old_path_rows, B),
        "AB_exact_A_confirmed_by_B": ("new_A_subset", A, AB_exact),
        "AB_near30m_DIAGNOSTIC": ("new_A_subset", A, AB_near30),
        "C_old_core_night_market_top20": ("old", old_path_rows, C),
    }
    windows = ["all", "core_20_08", "night_20_04", "dawn_04_08", "noncore"]

    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "generated_bjt": datetime.now(timezone.utc).astimezone(BJ).isoformat(),
        "method": "FIRST-LAYER DISCOVERY ONLY. Signal features are read only from score/snapshot tables at signal ts. Old radar does NOT read community_forward_outcomes. Labels are recomputed causally from post-signal OHLC: next complete 15m open, then 15m/30m/1h close_return and MFE. No managed_1h, no TP/SL, no execution search.",
        "sims": SIMS,
        "meta": {
            "new_score_meta": new_meta,
            "new_path_meta": new_path_meta,
            "old_score_meta": old_meta,
            "old_path_meta": old_path_meta,
            "candidate_counts": {"A": len(A), "B": len(B), "AB_exact": len(AB_exact), "AB_near30m": len(AB_near30), "C": len(C)},
        },
        "sets": {},
    }

    lines = [
        "# Full-data first-layer discovery validation",
        "",
        f"generated_bjt: `{result['generated_bjt']}`",
        "",
        result["method"],
        "",
        "## Coverage",
        "```text",
        f"new_rows={len(new_path_rows)} new_min={new_meta.get('start_utc')} new_latest={new_meta.get('latest_snapshot_utc')} new_complete_end={new_meta.get('complete_end_utc')}",
        f"old_rows={len(old_path_rows)} old_min={old_meta.get('min_ts')} old_max={old_meta.get('max_ts')} old_source={old_meta.get('source_table')}",
        f"candidate_counts A={len(A)} B={len(B)} AB_exact={len(AB_exact)} AB_near30m={len(AB_near30)} C={len(C)}",
        "```",
        "",
        "## Pass summary",
        "",
        "Legend: each flags cell is `15m/30m/1h`, C=close_return avg > same-ts random p95, M=MFE avg > same-ts random p95.",
        "",
        "```text",
    ]
    idx = 0
    for set_name, (_source_name, universe0, selected0) in set_defs.items():
        result["sets"].setdefault(set_name, {})
        for window in windows:
            universe = filter_window(universe0, window)
            selected = filter_window(selected0, window)
            block = summarize(set_name, universe, selected, seed=2026060300 + idx * 1000)
            idx += 1
            result["sets"][set_name][window] = block
            lines.append(line(set_name, window, block))
    lines.extend(["```", "", "## Key details", ""])
    for set_name in ["A_new_market_top20", "AB_exact_A_confirmed_by_B", "AB_near30m_DIAGNOSTIC", "B_old_watch_hot", "C_old_core_night_market_top20"]:
        for window in ["all", "core_20_08", "night_20_04", "dawn_04_08", "noncore"]:
            block = result["sets"][set_name][window]
            if block["rows"] >= 20 or set_name.startswith("AB_exact"):
                lines.extend(detail_lines(set_name, window, block))

    out = PROJECT_ROOT / "output" / f"full-data-first-layer-discovery-clean-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
