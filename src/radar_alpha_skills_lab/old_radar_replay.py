from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import OUTPUT_DIR
from .old_radar_alpha import CandidateRule, candidate_rows, load_old_radar_rows, _score
from .signal_control import CACHE_DIR, COSTS, fetch_exchange_symbols, iso, pct, stat, to_fapi_symbol

INTERVAL_MINUTES = 15
HARD_SL = -0.06
TP_HALF = 0.05
TP_HALF_SIZE = 0.5


def ceil_next_interval(dt: datetime, *, minutes: int = INTERVAL_MINUTES) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minute_bucket = (dt.minute // minutes) * minutes
    floored = dt.replace(minute=minute_bucket)
    if floored == dt:
        return dt
    return floored + timedelta(minutes=minutes)


def _bar_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def _entry_open(bars: Sequence[Sequence[Any]], signal_dt: datetime) -> tuple[float | None, datetime | None]:
    entry_dt = ceil_next_interval(signal_dt)
    entry_ms = int(entry_dt.timestamp() * 1000)
    for bar in bars:
        if int(bar[0]) >= entry_ms:
            return float(bar[1]), _bar_dt(bar)
    return None, None


def _path_until(bars: Sequence[Sequence[Any]], entry_dt: datetime, hours: float = 24.0) -> list[Sequence[Any]]:
    start_ms = int(entry_dt.timestamp() * 1000)
    end_ms = int((entry_dt + timedelta(hours=hours)).timestamp() * 1000)
    return [bar for bar in bars if start_ms <= int(bar[0]) <= end_ms]


def simulate_24h_exit(bars: Sequence[Sequence[Any]], signal_dt: datetime, *, strategy: str) -> dict[str, Any]:
    entry_px, actual_entry_dt = _entry_open(bars, signal_dt)
    if entry_px is None or actual_entry_dt is None or entry_px <= 0:
        return {"pnl": None, "reason": "no_entry", "entry_price": None, "entry_dt": None}
    path = _path_until(bars, actual_entry_dt, 24.0)
    if not path:
        return {"pnl": None, "reason": "no_path", "entry_price": entry_px, "entry_dt": iso(actual_entry_dt)}

    hard_sl_px = entry_px * (1 + HARD_SL)
    tp_px = entry_px * (1 + TP_HALF)
    end_ms = int((actual_entry_dt + timedelta(hours=24)).timestamp() * 1000)

    if strategy == "hold24h":
        exit_bar = path[-1]
        for bar in path:
            if int(bar[0]) >= end_ms:
                exit_bar = bar
                break
        pnl = float(exit_bar[4]) / entry_px - 1
        return {"pnl": pnl, "reason": "time_exit", "entry_price": entry_px, "entry_dt": iso(actual_entry_dt)}

    if strategy == "hard_sl6_hold24h":
        exit_bar = path[-1]
        for bar in path:
            if float(bar[3]) <= hard_sl_px:
                return {"pnl": HARD_SL, "reason": "hard_sl", "entry_price": entry_px, "entry_dt": iso(actual_entry_dt)}
            if int(bar[0]) >= end_ms:
                exit_bar = bar
                break
        pnl = float(exit_bar[4]) / entry_px - 1
        return {"pnl": pnl, "reason": "time_exit", "entry_price": entry_px, "entry_dt": iso(actual_entry_dt)}

    if strategy == "tp5_half_protect":
        realized = 0.0
        left = 1.0
        tp_hit = False
        for bar in path:
            low = float(bar[3])
            high = float(bar[2])
            close = float(bar[4])
            if tp_hit:
                if low <= entry_px:
                    return {
                        "pnl": realized,
                        "reason": "protect_after_tp",
                        "entry_price": entry_px,
                        "entry_dt": iso(actual_entry_dt),
                    }
            else:
                if low <= hard_sl_px:
                    return {"pnl": HARD_SL, "reason": "hard_sl", "entry_price": entry_px, "entry_dt": iso(actual_entry_dt)}
                if high >= tp_px:
                    realized += TP_HALF_SIZE * TP_HALF
                    left -= TP_HALF_SIZE
                    tp_hit = True
            if int(bar[0]) >= end_ms:
                return {
                    "pnl": realized + left * (close / entry_px - 1),
                    "reason": "tp_then_time" if tp_hit else "time_exit",
                    "entry_price": entry_px,
                    "entry_dt": iso(actual_entry_dt),
                }
        close = float(path[-1][4])
        return {
            "pnl": realized + left * (close / entry_px - 1),
            "reason": "tp_then_time" if tp_hit else "time_exit",
            "entry_price": entry_px,
            "entry_dt": iso(actual_entry_dt),
        }

    raise ValueError(f"unknown strategy: {strategy}")


def fetch_15m_klines(raw_symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cache = CACHE_DIR / f"{raw_symbol}-15m-{start_ms}-{end_ms}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    query = urllib.parse.urlencode(
        {"symbol": raw_symbol, "interval": "15m", "startTime": start_ms, "endTime": end_ms, "limit": 1500}
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


def fixed_old_shadow_rules() -> dict[str, CandidateRule]:
    return {
        "old_wait_entry_trigger_24h": CandidateRule(
            name="old_wait_entry_trigger_24h",
            horizon="24h",
            gate_field="recommended_action",
            gate_values=("wait_for_entry_trigger",),
        ),
        "old_core_night_mkt_top20_24h": CandidateRule(
            name="old_core_night_mkt_top20_24h",
            horizon="24h",
            gate_field="session",
            gate_values=("core_night",),
            score_field="market_confirmation_score",
            top_fraction=0.20,
        ),
    }


def _prepare_rows_for_replay(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tradable = fetch_exchange_symbols()
    prepared: list[dict[str, Any]] = []
    invalid: dict[str, int] = defaultdict(int)
    for row0 in rows:
        row = dict(row0)
        raw = to_fapi_symbol(str(row["symbol"]), tradable)
        if raw is None:
            invalid[str(row["symbol"])] += 1
            continue
        row["raw_symbol"] = raw
        prepared.append(row)
    return prepared, {"input_rows": len(rows), "tradable_rows": len(prepared), "invalid_symbols": dict(sorted(invalid.items()))}


def _top_symbol(rows: Sequence[Mapping[str, Any]], pnl_key: str, cost: float) -> dict[str, Any]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(pnl_key) is not None:
            by_sym[str(row["symbol"])].append(float(row[pnl_key]) - cost)
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    top5 = {s for s, _v, _n in ranked[:5]}
    return {
        "top_symbols": [{"symbol": s, "sum": v, "n": n} for s, v, n in ranked[:10]],
        "remove_top5_stat": stat(float(row[pnl_key]) - cost for row in rows if row.get(pnl_key) is not None and str(row["symbol"]) not in top5),
    }


def _cap_portfolio(rows: Sequence[Mapping[str, Any]], pnl_key: str, *, cap: int, cost: float, rank_key: str) -> dict[str, Any]:
    signals: list[tuple[datetime, float, str, datetime, float]] = []
    for row in rows:
        if row.get(pnl_key) is None:
            continue
        ent = row["ts_dt"]
        pnl = float(row[pnl_key]) - cost
        signals.append((ent, -_score(row, rank_key), str(row["symbol"]), ent + timedelta(hours=24), pnl))
    signals.sort(key=lambda x: (x[0], x[1], x[2]))
    active: list[datetime] = []
    chosen: list[float] = []
    skipped = 0
    for ent, _rank, _sym, exit_dt, pnl in signals:
        active = [ex for ex in active if ex > ent]
        if len(active) >= cap:
            skipped += 1
            continue
        active.append(exit_dt)
        chosen.append(pnl / cap)
    st = stat(chosen)
    return {"cap": cap, "taken": len(chosen), "skipped": skipped, "slot_stat": st, "comp": st["comp"], "mdd": st["mdd"]}


def summarize_replay(rows: Sequence[Mapping[str, Any]], *, rule: CandidateRule) -> dict[str, Any]:
    rank_key = rule.score_field or "final_score"
    out: dict[str, Any] = {
        "rows": len(rows),
        "symbols": len({str(r["symbol"]) for r in rows}),
        "rule": rule.__dict__,
        "strategies": {},
    }
    for strategy in ["hold24h", "hard_sl6_hold24h", "tp5_half_protect"]:
        key = f"pnl_{strategy}"
        valid = [r for r in rows if r.get(key) is not None]
        out["strategies"][strategy] = {
            "rows": len(valid),
            "symbols": len({str(r["symbol"]) for r in valid}),
            "all_taker8": stat(float(r[key]) - COSTS["all_taker_8bp_total"] for r in valid),
            "cap5": _cap_portfolio(valid, key, cap=5, cost=COSTS["all_taker_8bp_total"], rank_key=rank_key),
            "cap10": _cap_portfolio(valid, key, cap=10, cost=COSTS["all_taker_8bp_total"], rank_key=rank_key),
            "concentration": _top_symbol(valid, key, COSTS["all_taker_8bp_total"]),
            "exit_reasons": dict(sorted(_count_reasons(valid, f"reason_{strategy}").items())),
        }
    return out


def _count_reasons(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(key))] += 1
    return counts


def run_fixed_old_shadow_replay() -> dict[str, Any]:
    old_rows, old_meta = load_old_radar_rows()
    rules = fixed_old_shadow_rules()
    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Fixed old-radar 24h shadow replay on Binance USDT-M 15m OHLC; entry next 15m open; conservative stop/protect priority; all-taker 8bp total.",
        "input_meta": old_meta,
        "shadows": {},
        "fetch_errors": {},
    }
    for name, rule in rules.items():
        selected = candidate_rows(old_rows, rule)
        prepared, prep_meta = _prepare_rows_for_replay(selected)
        if prepared:
            fetch_start = min(r["ts_dt"] for r in prepared) - timedelta(minutes=30)
            fetch_end = max(r["ts_dt"] for r in prepared) + timedelta(hours=25)
        else:
            fetch_start = fetch_end = datetime.now(timezone.utc)
        bars_by_symbol: dict[str, list[list[Any]]] = {}
        errors: dict[str, str] = {}
        for raw in sorted({str(r["raw_symbol"]) for r in prepared}):
            try:
                bars_by_symbol[raw] = fetch_15m_klines(raw, fetch_start, fetch_end)
            except Exception as exc:  # noqa: BLE001
                errors[raw] = str(exc)[:300]
                bars_by_symbol[raw] = []
        replayed: list[dict[str, Any]] = []
        no_path = 0
        for row0 in prepared:
            row = dict(row0)
            bars = bars_by_symbol.get(str(row["raw_symbol"]), [])
            for strategy in ["hold24h", "hard_sl6_hold24h", "tp5_half_protect"]:
                sim = simulate_24h_exit(bars, row["ts_dt"], strategy=strategy)
                row[f"pnl_{strategy}"] = sim["pnl"]
                row[f"reason_{strategy}"] = sim["reason"]
                row[f"entry_price_{strategy}"] = sim["entry_price"]
            if row["pnl_hold24h"] is None:
                no_path += 1
            replayed.append(row)
        block = summarize_replay(replayed, rule=rule)
        block["prepare_meta"] = prep_meta
        block["path_meta"] = {
            "fetch_start_utc": iso(fetch_start),
            "fetch_end_utc": iso(fetch_end),
            "symbols_requested": len(bars_by_symbol),
            "symbols_with_errors": errors,
            "no_hold24h_path_rows": no_path,
        }
        result["shadows"][name] = block
    return result


def render_replay_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Fixed Old Radar 24h OHLC Replay",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        f"method: {result['method']}",
        "",
        "## Input",
        "",
        "```text",
        json.dumps(result["input_meta"], ensure_ascii=False, indent=2),
        "```",
    ]
    for name, shadow in result["shadows"].items():
        lines += ["", f"## {name}", "", "```text", json.dumps(shadow["prepare_meta"], ensure_ascii=False, indent=2), json.dumps(shadow["path_meta"], ensure_ascii=False, indent=2), "```"]
        for strategy, block in shadow["strategies"].items():
            st = block["all_taker8"]
            cap5 = block["cap5"]
            cap10 = block["cap10"]
            top = block["concentration"]["top_symbols"][:8]
            remove_top5 = block["concentration"]["remove_top5_stat"]
            lines += [
                "",
                f"### {strategy}",
                "",
                "```text",
                f"rows={block['rows']} symbols={block['symbols']}",
                f"all_taker sum={pct(st['sum'])} avg={pct(st['avg'])} median={pct(st['median'])} win={st['win']*100:.1f}% sharpe={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])} comp={pct(st['comp'])}",
                f"cap5 comp={pct(cap5['comp'])} sharpe={cap5['slot_stat']['sharpe_like']:+.2f} mdd={pct(cap5['mdd'])} taken={cap5['taken']} skipped={cap5['skipped']}",
                f"cap10 comp={pct(cap10['comp'])} sharpe={cap10['slot_stat']['sharpe_like']:+.2f} mdd={pct(cap10['mdd'])} taken={cap10['taken']} skipped={cap10['skipped']}",
                f"remove_top5 avg={pct(remove_top5['avg'])} sum={pct(remove_top5['sum'])}",
                "exit_reasons=" + json.dumps(block["exit_reasons"], ensure_ascii=False),
                "top_symbols=" + ", ".join(f"{x['symbol']} {pct(x['sum'])}/{x['n']}" for x in top),
                "```",
            ]
    return "\n".join(lines) + "\n"


def write_replay_outputs(result: Mapping[str, Any], out_prefix: str | Path) -> tuple[Path, Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(render_replay_report(result))
    return json_path, md_path
