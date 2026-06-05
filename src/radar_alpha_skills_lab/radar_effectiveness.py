from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import DEFAULT_SOURCE
from .old_radar_alpha import CandidateRule, candidate_rows as old_candidate_rows, load_old_radar_rows, parse_ts
from .old_radar_replay import ceil_next_interval, fetch_15m_klines
from .score_regime import bjt_session
from .signal_control import COSTS, fetch_exchange_symbols, iso, load_snapshot_rows, pct, stat, to_fapi_symbol

HORIZONS = {"15m": 1, "30m": 2, "1h": 4}


def _bar_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def simulate_short_horizons(bars: Sequence[Sequence[Any]], signal_dt: datetime) -> dict[str, Any]:
    """Return close-to-entry and MFE/MAE for 15m/30m/1h from next 15m open."""
    entry_dt = ceil_next_interval(signal_dt, minutes=15)
    path = [b for b in bars if int(b[0]) >= int(entry_dt.timestamp() * 1000)]
    if not path:
        return {"entry_dt": None, "entry_price": None, "reason": "no_entry"}
    entry = float(path[0][1])
    if entry <= 0:
        return {"entry_dt": iso(_bar_dt(path[0])), "entry_price": entry, "reason": "bad_entry"}
    out: dict[str, Any] = {"entry_dt": iso(_bar_dt(path[0])), "entry_price": entry, "reason": "ok"}
    for name, n_bars in HORIZONS.items():
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


def candidate_top_fraction_by_ts(rows: Sequence[Mapping[str, Any]], score_field: str, fraction: float) -> list[dict[str, Any]]:
    if fraction <= 0 or fraction > 1:
        raise ValueError("fraction must be in (0, 1]")
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["ts_dt"]].append(dict(row))
    out: list[dict[str, Any]] = []
    for _ts, group in sorted(grouped.items()):
        n = max(1, math.ceil(len(group) * fraction))
        out.extend(sorted(group, key=lambda r: (-_num(r.get(score_field)), str(r.get("symbol") or "")))[:n])
    return out


def _num(value: Any) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _values(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def _top_symbol(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(key) is not None:
            by_sym[str(row.get("symbol"))].append(float(row[key]))
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    top5 = {s for s, _v, _n in ranked[:5]}
    return {
        "top_symbols": [{"symbol": s, "sum": v, "n": n} for s, v, n in ranked[:10]],
        "remove_top5": stat(float(row[key]) for row in rows if row.get(key) is not None and str(row.get("symbol")) not in top5),
    }


def _positive_day_ratio(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(key) is not None:
            by_day[str(row.get("date_bjt"))].append(float(row[key]))
    sums = {day: sum(vals) for day, vals in by_day.items()}
    return {
        "days": len(sums),
        "positive_days": sum(1 for value in sums.values() if value > 0),
        "positive_day_ratio": (sum(1 for value in sums.values() if value > 0) / len(sums)) if sums else 0.0,
        "day_sums": dict(sorted(sums.items())),
    }


def _group_by_ts(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["ts_dt"]].append(dict(row))
    return grouped


def random_same_timestamp_distribution(
    universe_rows: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    key: str,
    *,
    sims: int = 200,
    seed: int = 20260601,
) -> dict[str, Any]:
    universe_by_ts = _group_by_ts(universe_rows)
    counts = {ts: len(group) for ts, group in _group_by_ts(candidate).items()}
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    for _ in range(sims):
        picked: list[dict[str, Any]] = []
        for ts, n in counts.items():
            pool = [r for r in universe_by_ts.get(ts, []) if r.get(key) is not None]
            pool = sorted(pool, key=lambda r: str(r.get("symbol") or ""))
            if not pool:
                continue
            picked.extend(pool if n >= len(pool) else rng.sample(pool, n))
        st = stat(float(r[key]) for r in picked if r.get(key) is not None)
        avgs.append(st["avg"])
        sums.append(st["sum"])

    def quantiles(values: list[float]) -> dict[str, float]:
        xs = sorted(values)
        if not xs:
            return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
        def q(p: float) -> float:
            return xs[min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))]
        return {"p50": q(0.50), "p90": q(0.90), "p95": q(0.95), "p99": q(0.99)}

    return {"sims": sims, "avg": quantiles(avgs), "sum": quantiles(sums)}


def attach_short_horizon_ohlc(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"input_rows": 0, "path_rows": 0, "symbols_requested": 0, "symbols_with_errors": {}}
    fetch_start = min(r["ts_dt"] for r in rows) - timedelta(minutes=30)
    fetch_end = max(r["ts_dt"] for r in rows) + timedelta(hours=2)
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
    no_1h = 0
    for row0 in rows:
        row = dict(row0)
        sim = simulate_short_horizons(by_symbol.get(str(row.get("raw_symbol")), []), row["ts_dt"])
        row.update(sim)
        if row.get("ret_1h") is None:
            no_1h += 1
            continue
        out.append(row)
    return out, {
        "input_rows": len(rows),
        "path_rows": len(out),
        "no_1h_rows": no_1h,
        "symbols_requested": len(symbols),
        "symbols_with_errors": errors,
        "fetch_start_utc": iso(fetch_start),
        "fetch_end_utc": iso(fetch_end),
        "entry_model": "next complete 15m Binance USDT-M open; close return and intrawindow MFE/MAE for 15m/30m/1h",
    }


def _summarize_candidate(universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], *, sims: int, seed: int) -> dict[str, Any]:
    block: dict[str, Any] = {"rows": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "horizons": {}}
    for h in ["15m", "30m", "1h"]:
        ret_key = f"ret_{h}"
        mfe_key = f"mfe_{h}"
        ret_stat = stat(_values(rows, ret_key))
        mfe_stat = stat(_values(rows, mfe_key))
        rand = random_same_timestamp_distribution(universe, rows, ret_key, sims=sims, seed=seed) if rows and len(rows) < len(universe) else {}
        rand_mfe = random_same_timestamp_distribution(universe, rows, mfe_key, sims=sims, seed=seed + 17) if rows and len(rows) < len(universe) else {}
        mfe_values = _values(rows, mfe_key)
        block["horizons"][h] = {
            "close_return": ret_stat,
            "mfe": mfe_stat,
            "mfe_hit_1pct": (sum(1 for v in mfe_values if v >= 0.01) / len(mfe_values)) if mfe_values else 0.0,
            "mfe_hit_2pct": (sum(1 for v in mfe_values if v >= 0.02) / len(mfe_values)) if mfe_values else 0.0,
            "close_above_random_p95_avg": (ret_stat["avg"] > rand.get("avg", {}).get("p95", float("inf"))) if rand else None,
            "mfe_above_random_p95_avg": (mfe_stat["avg"] > rand_mfe.get("avg", {}).get("p95", float("inf"))) if rand_mfe else None,
            "random_same_ts": rand,
            "random_same_ts_mfe": rand_mfe,
            "top_symbol": _top_symbol(rows, ret_key),
            "day": _positive_day_ratio(rows, ret_key),
        }
    return block


def _decorate_old_rows(rows: Sequence[Mapping[str, Any]], complete_1h_cutoff: datetime) -> list[dict[str, Any]]:
    tradable = fetch_exchange_symbols()
    out: list[dict[str, Any]] = []
    for row0 in rows:
        row = dict(row0)
        if row["ts_dt"] > complete_1h_cutoff:
            continue
        raw = to_fapi_symbol(str(row.get("symbol")), tradable)
        if raw is None:
            continue
        row["raw_symbol"] = raw
        row["family"] = str(row.get("decision_status"))
        out.append(row)
    return out


def build_new_candidate_sets(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "new_all_snapshot_basket": [dict(r) for r in rows],
        "new_unified_top20": candidate_top_fraction_by_ts(rows, "unified_discovery_score", 0.20),
        "new_market_top20": candidate_top_fraction_by_ts(rows, "market_confirmation_score", 0.20),
        "new_core_night_market_top20": candidate_top_fraction_by_ts(
            [dict(r) for r in rows if bjt_session(int(r.get("hour_bjt", 0))) == "core_night"], "market_confirmation_score", 0.20
        ),
    }


def build_old_candidate_sets(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "old_watch_hot": [dict(r) for r in rows if str(r.get("decision_status")) == "watch_hot"],
        "old_eligible": [dict(r) for r in rows if str(r.get("eligible_long")) == "eligible"],
        "old_market_top20": candidate_top_fraction_by_ts(rows, "market_confirmation_score", 0.20),
        "old_core_night_market_top20": candidate_top_fraction_by_ts(
            [dict(r) for r in rows if str(r.get("session")) == "core_night"], "market_confirmation_score", 0.20
        ),
    }


def run_radar_effectiveness(sims: int = 200, seed: int = 20260601) -> dict[str, Any]:
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    new_path_rows, new_path_meta = attach_short_horizon_ohlc(new_rows)
    old_rows, old_meta = load_old_radar_rows()
    cutoff = parse_ts(str(old_meta["complete_cutoffs"]["1h"]))
    old_ready = _decorate_old_rows(old_rows, cutoff)
    old_path_rows, old_path_meta = attach_short_horizon_ohlc(old_ready)

    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Radar effectiveness = after signal, next complete 15m Binance USDT-M open, close return/MFE over 15m/30m/1h. This validates discovery/rise, not entry-exit production alpha.",
        "new_radar": {"meta": new_meta, "path_meta": new_path_meta, "sets": {}},
        "old_radar": {"meta": old_meta, "path_meta": old_path_meta, "sets": {}},
    }
    for name, rows in build_new_candidate_sets(new_path_rows).items():
        result["new_radar"]["sets"][name] = _summarize_candidate(new_path_rows, rows, sims=sims, seed=seed)
    for name, rows in build_old_candidate_sets(old_path_rows).items():
        result["old_radar"]["sets"][name] = _summarize_candidate(old_path_rows, rows, sims=sims, seed=seed)
    return result


def _line_for(name: str, block: Mapping[str, Any]) -> list[str]:
    lines = [f"### {name}", "", "```text", f"rows={block['rows']} symbols={block['symbols']}"]
    for h in ["15m", "30m", "1h"]:
        hb = block["horizons"][h]
        st = hb["close_return"]
        mfe = hb["mfe"]
        rand = hb.get("random_same_ts") or {}
        rand_mfe = hb.get("random_same_ts_mfe") or {}
        p95 = rand.get("avg", {}).get("p95")
        mfe_p95 = rand_mfe.get("avg", {}).get("p95")
        rand_txt = "n/a" if p95 is None else pct(p95)
        rand_mfe_txt = "n/a" if mfe_p95 is None else pct(mfe_p95)
        flag = hb.get("close_above_random_p95_avg")
        mfe_flag = hb.get("mfe_above_random_p95_avg")
        lines.append(
            f"{h}: close avg={pct(st['avg'])} med={pct(st['median'])} win={st['win']*100:.1f}% sh={st['sharpe_like']:+.2f} "
            f"sum={pct(st['sum'])} | mfe avg={pct(mfe['avg'])} hit1={hb['mfe_hit_1pct']*100:.1f}% hit2={hb['mfe_hit_2pct']*100:.1f}% "
            f"| rand_close_p95={rand_txt} pass={flag} rand_mfe_p95={rand_mfe_txt} mfe_pass={mfe_flag}"
        )
    top = block["horizons"]["1h"]["top_symbol"]["top_symbols"][:6]
    lines.append("top_1h_symbols=" + ", ".join(f"{x['symbol']} {pct(x['sum'])}/{x['n']}" for x in top))
    lines.extend(["```", ""])
    return lines


def render_radar_effectiveness_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# New vs Old Radar Short-Horizon Effectiveness",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        result["method"],
        "",
        "## New radar input",
        "",
        "```text",
        json.dumps(result["new_radar"]["meta"], ensure_ascii=False, indent=2),
        json.dumps(result["new_radar"]["path_meta"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## New radar sets",
        "",
    ]
    for name, block in result["new_radar"]["sets"].items():
        lines.extend(_line_for(name, block))
    lines.extend([
        "## Old radar input",
        "",
        "```text",
        json.dumps(result["old_radar"]["meta"], ensure_ascii=False, indent=2),
        json.dumps(result["old_radar"]["path_meta"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Old radar sets",
        "",
    ])
    for name, block in result["old_radar"]["sets"].items():
        lines.extend(_line_for(name, block))
    return "\n".join(lines)


def write_radar_effectiveness_outputs(result: Mapping[str, Any], out_prefix: str | Path) -> tuple[Path, Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(render_radar_effectiveness_report(result))
    return json_path, md_path
