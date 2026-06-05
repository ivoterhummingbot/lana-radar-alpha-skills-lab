from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable, Mapping, Sequence

from .config import DEFAULT_SOURCE, OUTPUT_DIR, SourceConfig

BJ = timezone(timedelta(hours=8))
CACHE_DIR = OUTPUT_DIR / "klines_1m_cache" / "signal_control"

COSTS: dict[str, float] = {
    "gross_or_maker0": 0.0,
    "one_taker_4bp_total": 0.0004,
    "all_taker_8bp_total": 0.0008,
    "all_taker_8bp_plus_slip1bp_side": 0.0010,
    "all_taker_8bp_plus_slip2bp_side": 0.0012,
}

TP1 = 0.015
TP1_SIZE = 0.5
TP2 = 0.030
INITIAL_SL = -0.025
LOCK_AFTER_TP1 = 0.002


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def stat(vals: Iterable[float]) -> dict[str, Any]:
    xs = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if not xs:
        return {
            "n": 0,
            "sum": 0.0,
            "avg": 0.0,
            "median": 0.0,
            "win": 0.0,
            "sharpe_like": 0.0,
            "mdd": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "comp": 0.0,
        }
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    comp = 1.0
    for v in xs:
        eq += v
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
        comp *= 1.0 + v
    sd = pstdev(xs) if len(xs) > 1 else 0.0
    return {
        "n": len(xs),
        "sum": sum(xs),
        "avg": mean(xs),
        "median": median(xs),
        "win": sum(1 for v in xs if v > 0) / len(xs),
        "sharpe_like": mean(xs) / sd * math.sqrt(len(xs)) if sd > 1e-12 else 0.0,
        "mdd": mdd,
        "best": max(xs),
        "worst": min(xs),
        "comp": comp - 1.0,
    }


def cooldown_symbol(rows: Sequence[Mapping[str, Any]], minutes: int = 60) -> list[dict[str, Any]]:
    last: dict[str, datetime] = {}
    kept: list[dict[str, Any]] = []
    for row in sorted((dict(r) for r in rows), key=lambda r: (r["ts_dt"], str(r["symbol"]))):
        sym = str(row["symbol"])
        ts = row["ts_dt"]
        prev = last.get(sym)
        if prev is not None and ts - prev < timedelta(minutes=minutes):
            continue
        kept.append(row)
        last[sym] = ts
    return kept


def _group_by_ts(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[r["ts_dt"]].append(dict(r))
    return grouped


def _signal_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows if str(r.get("prev5m_stage") or "") != "preconfirmed_5m"]


def build_signal_and_controls(rows: Sequence[Mapping[str, Any]], seed: int = 20260601) -> dict[str, list[dict[str, Any]]]:
    """Build AlphaGBM/BPS-style with-signal and same-timestamp controls.

    The signal is the frozen new-radar `not_momentum_prev5m` rule. Controls are
    matched at each timestamp to avoid confusing basket/timestamp beta with rule alpha.
    """

    all_rows = [dict(r) for r in rows]
    signal = _signal_rows(all_rows)
    signal_cooldown = cooldown_symbol(signal, minutes=60)
    all_by_ts = _group_by_ts(all_rows)

    def matched_controls(reference_rows: Sequence[Mapping[str, Any]], salt: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        reference_by_ts = _group_by_ts(reference_rows)
        score_top: list[dict[str, Any]] = []
        random_rows: list[dict[str, Any]] = []
        rng = random.Random(seed + salt)
        for ts in sorted(reference_by_ts):
            n = len(reference_by_ts[ts])
            pool = list(all_by_ts.get(ts, []))
            score_top.extend(
                sorted(
                    pool,
                    key=lambda r: (-float(r.get("unified_discovery_score") or 0.0), str(r.get("symbol") or "")),
                )[:n]
            )
            pool_sorted = sorted(pool, key=lambda r: str(r.get("symbol") or ""))
            if n >= len(pool_sorted):
                random_rows.extend(pool_sorted)
            else:
                random_rows.extend(rng.sample(pool_sorted, n))
        return random_rows, score_top

    random_rows, score_top = matched_controls(signal, 0)
    random_cooldown, score_top_cooldown = matched_controls(signal_cooldown, 10_000)

    return {
        "signal_not_momentum": signal,
        "signal_not_momentum_cooldown1h": signal_cooldown,
        "control_random_matched": random_rows,
        "control_random_matched_cooldown1h": random_cooldown,
        "control_score_top_matched": score_top,
        "control_score_top_matched_cooldown1h": score_top_cooldown,
        "control_all_snapshots": all_rows,
        "control_all_snapshots_cooldown1h": cooldown_symbol(all_rows, minutes=60),
        "ablation_prev5m_preconfirmed": [
            dict(r) for r in all_rows if str(r.get("prev5m_stage") or "") == "preconfirmed_5m"
        ],
    }


def cap_portfolio(
    rows: Sequence[Mapping[str, Any]],
    pnl_key: str,
    cost: float,
    cap: int,
    rank_key: str = "unified_discovery_score",
) -> dict[str, Any]:
    sortable: list[tuple[datetime, float, str, datetime, float]] = []
    for r in rows:
        ts = r["ts_dt"]
        pnl = float(r[pnl_key]) - cost
        rank = float(r.get(rank_key) or 0.0)
        sortable.append((ts, -rank, str(r["symbol"]), ts + timedelta(hours=1), pnl))
    sortable.sort(key=lambda x: (x[0], x[1], x[2]))

    active: list[tuple[datetime, str]] = []
    chosen: list[float] = []
    taken_symbols: list[str] = []
    skipped = 0
    for ent, _neg_rank, sym, ex, pnl in sortable:
        active = [(aex, asym) for aex, asym in active if aex > ent]
        if len(active) >= cap:
            skipped += 1
            continue
        active.append((ex, sym))
        chosen.append(pnl / cap)
        taken_symbols.append(sym)
    st = stat(chosen)
    return {
        "cap": cap,
        "rank_key": rank_key,
        "taken": len(chosen),
        "skipped": skipped,
        "taken_symbols": taken_symbols,
        "slot_stat": st,
        "comp": st["comp"],
        "mdd": st["mdd"],
    }


def ceil_next_minute(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    floored = dt.replace(second=0, microsecond=0)
    return floored if dt == floored else floored + timedelta(minutes=1)


def fetch_exchange_symbols() -> set[str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "fapi_exchange_symbols.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
        return set(json.loads(cache.read_text()))
    req = urllib.request.Request(
        "https://fapi.binance.com/fapi/v1/exchangeInfo",
        headers={"User-Agent": "lana-radar-alpha-skills-lab/0.1"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    symbols = sorted(
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    )
    cache.write_text(json.dumps(symbols))
    return set(symbols)


def to_fapi_symbol(symbol: str, tradable: set[str]) -> str | None:
    s = str(symbol).upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{2,25}", s):
        return None
    raw = s if s.endswith("USDT") else f"{s}USDT"
    return raw if raw in tradable else None


def fetch_1m_klines(raw_symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cache = CACHE_DIR / f"{raw_symbol}-{start_ms}-{end_ms}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    query = urllib.parse.urlencode(
        {"symbol": raw_symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1500}
    )
    req = urllib.request.Request(
        "https://fapi.binance.com/fapi/v1/klines?" + query,
        headers={"User-Agent": "lana-radar-alpha-skills-lab/0.1"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(str(data)[:200])
    cache.write_text(json.dumps(data))
    time.sleep(0.025)
    return data


def _entry_open(bars: Sequence[Sequence[Any]], entry_dt: datetime) -> tuple[float | None, datetime | None]:
    start_ms = int(ceil_next_minute(entry_dt).timestamp() * 1000)
    for bar in bars:
        if int(bar[0]) >= start_ms:
            return float(bar[1]), datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)
    return None, None


def simulate_managed_1h(bars: Sequence[Sequence[Any]], entry_dt: datetime) -> dict[str, Any]:
    entry_px, actual_entry_dt = _entry_open(bars, entry_dt)
    if entry_px is None or actual_entry_dt is None or entry_px <= 0:
        return {"pnl": None, "reason": "no_entry", "entry_price": None}

    start_ms = int(actual_entry_dt.timestamp() * 1000)
    end_ms = int((entry_dt + timedelta(hours=1)).timestamp() * 1000)
    path = [b for b in bars if start_ms <= int(b[0]) <= end_ms]
    if not path:
        return {"pnl": None, "reason": "no_path", "entry_price": entry_px}

    tp1_px = entry_px * (1 + TP1)
    tp2_px = entry_px * (1 + TP2)
    hard_sl_px = entry_px * (1 + INITIAL_SL)
    lock_px = entry_px * (1 + LOCK_AFTER_TP1)
    pnl = 0.0
    left = 1.0
    tp1_hit = False
    for bar in path:
        high = float(bar[2])
        low = float(bar[3])
        close = float(bar[4])
        close_time_ms = int(bar[6])
        if tp1_hit:
            if low <= lock_px:
                pnl += left * LOCK_AFTER_TP1
                return {"pnl": pnl, "reason": "lock_after_tp1", "entry_price": entry_px}
        else:
            if low <= hard_sl_px:
                return {"pnl": INITIAL_SL, "reason": "initial_stop", "entry_price": entry_px}
        if not tp1_hit and high >= tp1_px:
            pnl += TP1_SIZE * TP1
            left -= TP1_SIZE
            tp1_hit = True
        if high >= tp2_px:
            pnl += left * TP2
            return {"pnl": pnl, "reason": "tp2_after_tp1" if tp1_hit else "tp2_full", "entry_price": entry_px}
        if close_time_ms >= end_ms:
            pnl += left * (close / entry_px - 1)
            return {"pnl": pnl, "reason": "tp1_then_time" if tp1_hit else "time_exit", "entry_price": entry_px}

    close = float(path[-1][4])
    pnl += left * (close / entry_px - 1)
    return {"pnl": pnl, "reason": "tp1_then_time" if tp1_hit else "time_exit", "entry_price": entry_px}


def _latest_snapshot_ts(source: SourceConfig) -> datetime:
    with sqlite3.connect(f"file:{source.maker_attention_db}?mode=ro", uri=True) as con:
        row = con.execute("select max(ts) from maker_attn_symbol_scores").fetchone()
    if not row or not row[0]:
        raise RuntimeError("maker_attn_symbol_scores has no max(ts)")
    return parse_ts(row[0])


def load_snapshot_rows(
    source: SourceConfig = DEFAULT_SOURCE,
    hours: float | None = None,
    end_utc: datetime | None = None,
    now_utc: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = now_utc or datetime.now(timezone.utc)
    latest = _latest_snapshot_ts(source)
    complete_end = min(end_utc or latest, latest, now - timedelta(hours=1, minutes=2))
    start = complete_end - timedelta(hours=hours) if hours else datetime(1970, 1, 1, tzinfo=timezone.utc)

    query = """
        select ts, symbol, discovery_family as family, priority_bucket, maker_attention_score,
               community_heat_score, market_confirmation_score, momentum_confirmation_score,
               unified_discovery_score, warning_score, fomo_risk_score, freshness_score,
               source_quality_score, attention_spread_score,
               prev5m_ret, prev5m_volume_ratio, prev5m_vwap_distance,
               prev5m_upper_wick_ratio, prev5m_confirmation_score,
               prev5m_confirmation_stage as prev5m_stage,
               btc_regime_state as btc_state, btc_5m_ret, btc_15m_ret, btc_1h_ret,
               symbol_rel_5m_vs_btc as symbol_rel5_vs_btc,
               btc_relative_gate_permission as btc_gate_permission
        from maker_attn_symbol_scores
        where ts >= ? and ts <= ?
        order by ts, symbol
    """
    with sqlite3.connect(f"file:{source.maker_attention_db}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(query, (iso(start), iso(complete_end)))]

    tradable = fetch_exchange_symbols()
    invalid_symbols = 0
    prepared: list[dict[str, Any]] = []
    for r in rows:
        ts_dt = parse_ts(str(r["ts"]))
        raw = to_fapi_symbol(str(r["symbol"]), tradable)
        if raw is None:
            invalid_symbols += 1
            continue
        r["ts_dt"] = ts_dt
        r["ts_bjt"] = ts_dt.astimezone(BJ).strftime("%Y-%m-%d %H:%M")
        r["date_bjt"] = ts_dt.astimezone(BJ).strftime("%Y-%m-%d")
        r["hour_bjt"] = ts_dt.astimezone(BJ).hour
        r["raw_symbol"] = raw
        prepared.append(r)

    meta = {
        "latest_snapshot_utc": iso(latest),
        "complete_end_utc": iso(complete_end),
        "start_utc": iso(start),
        "raw_rows": len(rows),
        "tradable_rows_before_path": len(prepared),
        "invalid_symbol_rows": invalid_symbols,
    }
    return prepared, meta


def attach_managed_1h(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"path_rows": 0, "symbols_requested": 0, "symbols_with_errors": {}}
    min_dt = min(r["ts_dt"] for r in rows)
    max_dt = max(r["ts_dt"] for r in rows)
    fetch_start = min_dt - timedelta(minutes=2)
    fetch_end = max_dt + timedelta(hours=1, minutes=5)
    by_symbol: dict[str, list[list[Any]]] = {}
    errors: dict[str, str] = {}
    for raw in sorted({str(r["raw_symbol"]) for r in rows}):
        try:
            by_symbol[raw] = fetch_1m_klines(raw, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001
            errors[raw] = str(exc)
            by_symbol[raw] = []

    out: list[dict[str, Any]] = []
    no_path = 0
    for r0 in rows:
        r = dict(r0)
        sim = simulate_managed_1h(by_symbol.get(str(r["raw_symbol"]), []), r["ts_dt"])
        if sim["pnl"] is None:
            no_path += 1
            continue
        r["managed_1h"] = float(sim["pnl"])
        r["managed_1h_reason"] = sim["reason"]
        r["entry_price"] = sim["entry_price"]
        out.append(r)
    return out, {
        "path_rows": len(out),
        "no_path_rows": no_path,
        "symbols_requested": len(by_symbol),
        "symbols_with_errors": errors,
        "fetch_start_utc": iso(fetch_start),
        "fetch_end_utc": iso(fetch_end),
        "entry_model": "next full 1m Binance USDT-M bar open after score ts; managed_1h stop-first conservative replay",
    }


def _stats_for_rows(rows: Sequence[Mapping[str, Any]], cost: float) -> dict[str, Any]:
    return stat(float(r["managed_1h"]) - cost for r in rows)


def _top_symbol_removal(rows: Sequence[Mapping[str, Any]], cost: float) -> dict[str, Any]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_sym[str(r["symbol"])].append(float(r["managed_1h"]) - cost)
    ranked = sorted(((sym, sum(vals), len(vals)) for sym, vals in by_sym.items()), key=lambda x: x[1], reverse=True)
    result: dict[str, Any] = {"top_symbols": [{"symbol": s, "sum": v, "n": n} for s, v, n in ranked[:10]]}
    for k in [1, 3, 5, 10]:
        removed = {s for s, _v, _n in ranked[:k]}
        result[f"remove_top{k}_symbols"] = {
            "removed": sorted(removed),
            "stat": stat(float(r["managed_1h"]) - cost for r in rows if str(r["symbol"]) not in removed),
        }
    return result


def _split_stats(rows: Sequence[Mapping[str, Any]], key: str, cost: float) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        grouped[str(r.get(key))].append(float(r["managed_1h"]) - cost)
    return {k: stat(vs) for k, vs in sorted(grouped.items())}


def summarize_sets(sets: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, rows in sets.items():
        block: dict[str, Any] = {
            "rows": len(rows),
            "symbols": len({str(r["symbol"]) for r in rows}),
            "costs": {},
        }
        for cost_name, cost in COSTS.items():
            c: dict[str, Any] = {"stat": _stats_for_rows(rows, cost)}
            if rows:
                c["top_symbol_removal"] = _top_symbol_removal(rows, cost)
                c["by_hour_bjt"] = _split_stats(rows, "hour_bjt", cost)
                c["by_date_bjt"] = _split_stats(rows, "date_bjt", cost)
            if name.endswith("cooldown1h") and rows and cost_name in {"gross_or_maker0", "one_taker_4bp_total", "all_taker_8bp_total"}:
                c["capacity"] = {
                    f"cap{cap}": cap_portfolio(rows, "managed_1h", cost, cap)
                    for cap in [5, 10, 20]
                }
            block["costs"][cost_name] = c
        summary[name] = block
    return summary


def random_baseline_distribution(
    rows: Sequence[Mapping[str, Any]],
    signal_counts_by_ts: Mapping[datetime, int],
    sims: int = 1000,
    seed: int = 20260601,
) -> dict[str, Any]:
    all_by_ts = _group_by_ts(rows)
    rng = random.Random(seed)
    avg_values: list[float] = []
    sum_values: list[float] = []
    for _ in range(sims):
        picked: list[dict[str, Any]] = []
        for ts, n in signal_counts_by_ts.items():
            pool = sorted(all_by_ts.get(ts, []), key=lambda r: str(r.get("symbol") or ""))
            if n >= len(pool):
                picked.extend(pool)
            else:
                picked.extend(rng.sample(pool, n))
        st = _stats_for_rows(picked, 0.0)
        avg_values.append(st["avg"])
        sum_values.append(st["sum"])
    avg_sorted = sorted(avg_values)
    sum_sorted = sorted(sum_values)

    def q(xs: list[float], pct: float) -> float:
        if not xs:
            return 0.0
        idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * pct))))
        return xs[idx]

    return {
        "sims": sims,
        "avg": {"p50": q(avg_sorted, 0.50), "p90": q(avg_sorted, 0.90), "p95": q(avg_sorted, 0.95), "p99": q(avg_sorted, 0.99)},
        "sum": {"p50": q(sum_sorted, 0.50), "p90": q(sum_sorted, 0.90), "p95": q(sum_sorted, 0.95), "p99": q(sum_sorted, 0.99)},
    }


def run_signal_control_audit(
    source: SourceConfig = DEFAULT_SOURCE,
    hours: float | None = None,
    sims: int = 1000,
    seed: int = 20260601,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    snapshot_rows, load_meta = load_snapshot_rows(source=source, hours=hours, now_utc=now_utc)
    path_rows, path_meta = attach_managed_1h(snapshot_rows)
    sets = build_signal_and_controls(path_rows, seed=seed)
    signal_counts_by_ts = {ts: len(group) for ts, group in _group_by_ts(sets["signal_not_momentum_cooldown1h"]).items()}
    rand_dist = random_baseline_distribution(path_rows, signal_counts_by_ts, sims=sims, seed=seed) if path_rows else {}
    summary = summarize_sets(sets)

    signal = summary["signal_not_momentum_cooldown1h"]["costs"]["gross_or_maker0"]["stat"]
    random_ctl = summary["control_random_matched_cooldown1h"]["costs"]["gross_or_maker0"]["stat"]
    score_ctl = summary["control_score_top_matched_cooldown1h"]["costs"]["gross_or_maker0"]["stat"]
    verdict = {
        "signal_vs_random_avg_delta": signal["avg"] - random_ctl["avg"],
        "signal_vs_score_top_avg_delta": signal["avg"] - score_ctl["avg"],
        "signal_above_random_p95_avg": signal["avg"] > rand_dist.get("avg", {}).get("p95", float("inf")),
    }
    if score_ctl["avg"] > signal["avg"] and score_ctl["win"] >= signal["win"]:
        verdict["primary_read"] = "SCORE_RANKING_BEATS_NOT_MOMENTUM_FILTER"
    elif signal["avg"] > random_ctl["avg"] and verdict["signal_above_random_p95_avg"]:
        verdict["primary_read"] = "NOT_MOMENTUM_FILTER_ALPHA_CONFIRMED_VS_RANDOM"
    elif signal["avg"] > random_ctl["avg"]:
        verdict["primary_read"] = "NOT_MOMENTUM_FILTER_WEAK_EDGE_NOT_RANDOM_P95"
    else:
        verdict["primary_read"] = "TIMESTAMP_OR_BASKET_BETA_DOMINATES"

    return {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "AlphaGBM BPS-style signal/control audit on new-radar snapshots",
        "load_meta": load_meta,
        "path_meta": path_meta,
        "cost_model": COSTS,
        "sets": summary,
        "random_baseline_distribution": rand_dist,
        "verdict": verdict,
    }


def pct(x: Any, digits: int = 2) -> str:
    try:
        f = float(x)
    except Exception:
        return "na"
    if not math.isfinite(f):
        return "na"
    return f"{f * 100:+.{digits}f}%"


def fmt_stat(s: Mapping[str, Any]) -> str:
    if not s or not s.get("n"):
        return "n=0"
    return (
        f"n={int(s['n']):<5d} sum={pct(s['sum'])} avg={pct(s['avg'])} med={pct(s['median'])} "
        f"win={float(s['win'])*100:5.1f}% sh={float(s['sharpe_like']):+5.2f} "
        f"mdd={pct(s['mdd'])} best={pct(s['best'])} worst={pct(s['worst'])} comp={pct(s['comp'])}"
    )


def render_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# New Radar Snapshot Signal/Control Audit",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "Method: AlphaGBM BPS-style signal/control audit.",
        "",
        "## Input",
        "",
        "```text",
        json.dumps({"load_meta": result["load_meta"], "path_meta": result["path_meta"]}, ensure_ascii=False, indent=2)[:3000],
        "```",
        "",
        "## Main comparison: 1h symbol cooldown, managed_1h",
        "",
        "```text",
    ]
    main_sets = [
        "signal_not_momentum_cooldown1h",
        "control_random_matched_cooldown1h",
        "control_score_top_matched_cooldown1h",
        "control_all_snapshots_cooldown1h",
        "ablation_prev5m_preconfirmed",
    ]
    for name in main_sets:
        block = result["sets"].get(name)
        if not block:
            continue
        lines.append(f"[{name}] rows={block['rows']} symbols={block['symbols']}")
        for cost in ["gross_or_maker0", "one_taker_4bp_total", "all_taker_8bp_total", "all_taker_8bp_plus_slip2bp_side"]:
            lines.append(f"  {cost:<34} {fmt_stat(block['costs'][cost]['stat'])}")
        lines.append("")
    lines.extend(["```", "", "## Random same-timestamp baseline distribution", "", "```text"])
    dist = result.get("random_baseline_distribution", {})
    lines.append(json.dumps(dist, ensure_ascii=False, indent=2))
    lines.extend(["```", "", "## Capacity: score-ranked cap portfolios", ""])
    for name in ["signal_not_momentum_cooldown1h", "control_random_matched_cooldown1h", "control_score_top_matched_cooldown1h"]:
        block = result["sets"].get(name, {})
        lines.append(f"### {name}")
        lines.append("```text")
        for cost in ["gross_or_maker0", "one_taker_4bp_total", "all_taker_8bp_total"]:
            c = block.get("costs", {}).get(cost, {})
            for cap_name, cap in c.get("capacity", {}).items():
                lines.append(
                    f"{cost:<28} {cap_name:<5} taken={cap['taken']:<5d} skipped={cap['skipped']:<5d} "
                    f"comp={pct(cap['comp'])} mdd={pct(cap['mdd'])}"
                )
        lines.append("```")
        lines.append("")

    lines.extend(["## Concentration: gross remove top symbols", ""])
    for name in ["signal_not_momentum_cooldown1h", "control_score_top_matched_cooldown1h"]:
        tsr = result["sets"][name]["costs"]["gross_or_maker0"].get("top_symbol_removal", {})
        lines.append(f"### {name}")
        lines.append("```text")
        top = ", ".join(f"{r['symbol']} {pct(r['sum'])}/{r['n']}" for r in tsr.get("top_symbols", [])[:8])
        lines.append("top_symbols: " + top)
        for key in ["remove_top1_symbols", "remove_top3_symbols", "remove_top5_symbols", "remove_top10_symbols"]:
            lines.append(f"{key:<22} {fmt_stat(tsr.get(key, {}).get('stat', {}))}")
        lines.append("```")
        lines.append("")

    lines.extend(["## Verdict", "", "```text"])
    lines.append(json.dumps(result["verdict"], ensure_ascii=False, indent=2))
    lines.extend(["```", ""])
    return "\n".join(lines)


def write_outputs(result: Mapping[str, Any], out_prefix: Path) -> tuple[Path, Path]:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    md_path.write_text(render_report(result))
    return json_path, md_path
