from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import DEFAULT_SOURCE
from .old_radar_alpha import load_old_radar_rows, parse_ts
from .old_radar_replay import ceil_next_interval, fetch_15m_klines
from .radar_effectiveness import _decorate_old_rows, build_new_candidate_sets, build_old_candidate_sets, random_same_timestamp_distribution
from .signal_control import COSTS, iso, load_snapshot_rows, pct, stat


@dataclass(frozen=True)
class ExecutionRule:
    name: str
    max_minutes: int
    tp: float | None = None
    sl: float | None = None
    protect_after_tp: float | None = None


def _bar_dt(bar: Sequence[Any]) -> datetime:
    return datetime.fromtimestamp(int(bar[0]) / 1000, tz=timezone.utc)


def _path_from_entry(bars: Sequence[Sequence[Any]], signal_dt: datetime, max_minutes: int) -> tuple[float | None, datetime | None, list[Sequence[Any]]]:
    entry_dt = ceil_next_interval(signal_dt, minutes=15)
    entry_ms = int(entry_dt.timestamp() * 1000)
    end_ms = int((entry_dt + timedelta(minutes=max_minutes)).timestamp() * 1000)
    path = [bar for bar in bars if entry_ms <= int(bar[0]) < end_ms]
    if not path:
        return None, None, []
    entry = float(path[0][1])
    return entry, _bar_dt(path[0]), path


def simulate_execution_rule(bars: Sequence[Sequence[Any]], signal_dt: datetime, rule: ExecutionRule) -> dict[str, Any]:
    """Conservative 15m OHLC replay for short hotcoin execution nodes.

    Priority inside a bar is stop/protect first, then TP. This prevents optimistic
    TP fills when a candle spans both the stop and target.
    """
    entry, actual_entry_dt, path = _path_from_entry(bars, signal_dt, rule.max_minutes)
    if entry is None or actual_entry_dt is None or entry <= 0:
        return {"pnl": None, "reason": "no_entry", "entry_dt": None, "entry_price": None}
    tp_px = entry * (1 + rule.tp) if rule.tp is not None else None
    sl_px = entry * (1 + rule.sl) if rule.sl is not None else None
    protect_px = entry * (1 + rule.protect_after_tp) if rule.protect_after_tp is not None else None
    tp_hit = False
    mfe = max(float(bar[2]) for bar in path) / entry - 1.0
    mae = min(float(bar[3]) for bar in path) / entry - 1.0
    for bar in path:
        low = float(bar[3])
        high = float(bar[2])
        if tp_hit and protect_px is not None and low <= protect_px:
            return {
                "pnl": rule.protect_after_tp,
                "reason": "protect_after_tp",
                "entry_dt": iso(actual_entry_dt),
                "entry_price": entry,
                "mfe": mfe,
                "mae": mae,
            }
        if not tp_hit and sl_px is not None and low <= sl_px:
            return {"pnl": rule.sl, "reason": "sl", "entry_dt": iso(actual_entry_dt), "entry_price": entry, "mfe": mfe, "mae": mae}
        if not tp_hit and tp_px is not None and high >= tp_px:
            if rule.protect_after_tp is None:
                return {"pnl": rule.tp, "reason": "tp", "entry_dt": iso(actual_entry_dt), "entry_price": entry, "mfe": mfe, "mae": mae}
            tp_hit = True
    pnl = float(path[-1][4]) / entry - 1.0
    return {
        "pnl": pnl,
        "reason": "tp_then_time" if tp_hit else "time_exit",
        "entry_dt": iso(actual_entry_dt),
        "entry_price": entry,
        "mfe": mfe,
        "mae": mae,
    }


def execution_rules() -> list[ExecutionRule]:
    return [
        ExecutionRule("time15", 15),
        ExecutionRule("time30", 30),
        ExecutionRule("time60", 60),
        ExecutionRule("tp08_sl2_30", 30, tp=0.008, sl=-0.02),
        ExecutionRule("tp12_sl2_30", 30, tp=0.012, sl=-0.02),
        ExecutionRule("tp15_sl3_60", 60, tp=0.015, sl=-0.03),
        ExecutionRule("tp20_sl3_60", 60, tp=0.020, sl=-0.03),
        ExecutionRule("tp30_sl4_60", 60, tp=0.030, sl=-0.04),
        ExecutionRule("tp12_protect0_60", 60, tp=0.012, sl=-0.03, protect_after_tp=0.0),
        ExecutionRule("tp20_protect05_60", 60, tp=0.020, sl=-0.03, protect_after_tp=0.005),
    ]


def attach_execution_rules(rows: Sequence[Mapping[str, Any]], rules: Sequence[ExecutionRule]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"input_rows": 0, "path_rows": 0, "symbols_requested": 0, "symbols_with_errors": {}}
    max_minutes = max(rule.max_minutes for rule in rules)
    fetch_start = min(r["ts_dt"] for r in rows) - timedelta(minutes=30)
    fetch_end = max(r["ts_dt"] for r in rows) + timedelta(minutes=max_minutes + 30)
    by_symbol: dict[str, list[list[Any]]] = {}
    errors: dict[str, str] = {}
    symbols = sorted({str(r["raw_symbol"]) for r in rows if r.get("raw_symbol")})
    for raw in symbols:
        try:
            by_symbol[raw] = fetch_15m_klines(raw, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001
            errors[raw] = str(exc)[:240]
            by_symbol[raw] = []
    out: list[dict[str, Any]] = []
    no_path = 0
    for row0 in rows:
        row = dict(row0)
        bars = by_symbol.get(str(row.get("raw_symbol")), [])
        ok = False
        for rule in rules:
            sim = simulate_execution_rule(bars, row["ts_dt"], rule)
            row[f"pnl_{rule.name}"] = sim["pnl"]
            row[f"reason_{rule.name}"] = sim["reason"]
            row[f"entry_dt_{rule.name}"] = sim["entry_dt"]
            row[f"entry_price_{rule.name}"] = sim["entry_price"]
            if sim.get("mfe") is not None:
                row[f"mfe_{rule.name}"] = sim["mfe"]
                row[f"mae_{rule.name}"] = sim["mae"]
            ok = ok or sim["pnl"] is not None
        if ok:
            out.append(row)
        else:
            no_path += 1
    return out, {
        "input_rows": len(rows),
        "path_rows": len(out),
        "no_path_rows": no_path,
        "symbols_requested": len(symbols),
        "symbols_with_errors": errors,
        "fetch_start_utc": iso(fetch_start),
        "fetch_end_utc": iso(fetch_end),
        "entry_model": "next complete 15m Binance USDT-M open; conservative stop/protect first inside each 15m bar",
    }


def cap_portfolio_by_entry(
    rows: Sequence[Mapping[str, Any]],
    pnl_key: str,
    *,
    cap: int,
    hold_minutes: int,
    cost: float,
    rank_key: str,
) -> dict[str, Any]:
    signals: list[tuple[datetime, float, str, datetime, float]] = []
    for row in rows:
        if row.get(pnl_key) is None:
            continue
        ent = row["ts_dt"]
        try:
            rank = -float(row.get(rank_key) or 0.0)
        except Exception:
            rank = 0.0
        signals.append((ent, rank, str(row.get("symbol") or ""), ent + timedelta(minutes=hold_minutes), float(row[pnl_key]) - cost))
    signals.sort(key=lambda x: (x[0], x[1], x[2]))
    active: list[datetime] = []
    chosen: list[float] = []
    skipped = 0
    for ent, _rank, _symbol, exit_dt, pnl in signals:
        active = [ex for ex in active if ex > ent]
        if len(active) >= cap:
            skipped += 1
            continue
        active.append(exit_dt)
        chosen.append(pnl / cap)
    st = stat(chosen)
    return {"cap": cap, "taken": len(chosen), "skipped": skipped, "slot_stat": st, "comp": st["comp"], "mdd": st["mdd"]}


def _top_symbol(rows: Sequence[Mapping[str, Any]], pnl_key: str, cost: float) -> dict[str, Any]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(pnl_key) is not None:
            by_sym[str(row.get("symbol"))].append(float(row[pnl_key]) - cost)
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    removed = {s for s, _v, _n in ranked[:5]}
    return {
        "top_symbols": [{"symbol": s, "sum": v, "n": n} for s, v, n in ranked[:10]],
        "remove_top5_stat": stat(float(row[pnl_key]) - cost for row in rows if row.get(pnl_key) is not None and str(row.get("symbol")) not in removed),
    }


def _reason_counts(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(key))] += 1
    return dict(sorted(counts.items()))


def _rank_key_for_set(name: str) -> str:
    if "market" in name:
        return "market_confirmation_score"
    if name.startswith("new"):
        return "unified_discovery_score"
    return "final_score"


def _summarize_set(universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], rules: Sequence[ExecutionRule], *, sims: int, seed: int, set_name: str) -> dict[str, Any]:
    out: dict[str, Any] = {"rows": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "rules": {}}
    cost = COSTS["all_taker_8bp_total"]
    rank_key = _rank_key_for_set(set_name)
    for rule in rules:
        key = f"pnl_{rule.name}"
        valid = [r for r in rows if r.get(key) is not None]
        st = stat(float(r[key]) - cost for r in valid)
        rand = random_same_timestamp_distribution(universe, valid, key, sims=sims, seed=seed) if valid and len(valid) < len(universe) else {}
        if rand.get("avg"):
            rand = dict(rand)
            rand["avg"] = {k: v - cost for k, v in rand["avg"].items()}
            rand["cost_model"] = "all candidates and random baseline subtract all-taker 8bp from avg quantiles"
        rand_p95 = rand.get("avg", {}).get("p95")
        out["rules"][rule.name] = {
            "rule": rule.__dict__,
            "rows": len(valid),
            "all_taker8": st,
            "above_random_p95_avg": (st["avg"] > rand_p95) if rand_p95 is not None else None,
            "random_same_ts": rand,
            "cap5": cap_portfolio_by_entry(valid, key, cap=5, hold_minutes=rule.max_minutes, cost=cost, rank_key=rank_key),
            "cap10": cap_portfolio_by_entry(valid, key, cap=10, hold_minutes=rule.max_minutes, cost=cost, rank_key=rank_key),
            "top_symbol": _top_symbol(valid, key, cost),
            "exit_reasons": _reason_counts(valid, f"reason_{rule.name}"),
        }
    return out


def _best_rules(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for name, rule_block in block["rules"].items():
        st = rule_block["all_taker8"]
        cap5 = rule_block["cap5"]
        ranked.append(
            {
                "rule": name,
                "avg": st["avg"],
                "sharpe_like": st["sharpe_like"],
                "comp": st["comp"],
                "mdd": st["mdd"],
                "cap5_comp": cap5["comp"],
                "cap5_sharpe": cap5["slot_stat"]["sharpe_like"],
                "above_random_p95_avg": rule_block["above_random_p95_avg"],
            }
        )
    return sorted(ranked, key=lambda x: (x["cap5_comp"], x["avg"], x["sharpe_like"]), reverse=True)[:5]


def run_execution_node_search(sims: int = 300, seed: int = 20260601) -> dict[str, Any]:
    rules = execution_rules()
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    new_exec_rows, new_path_meta = attach_execution_rules(new_rows, rules)

    old_rows, old_meta = load_old_radar_rows()
    cutoff = parse_ts(str(old_meta["complete_cutoffs"]["1h"]))
    old_ready = _decorate_old_rows(old_rows, cutoff)
    old_exec_rows, old_path_meta = attach_execution_rules(old_ready, rules)

    new_sets = build_new_candidate_sets(new_exec_rows)
    old_sets = build_old_candidate_sets(old_exec_rows)
    selected_sets = {
        "new_market_top20": ("new_radar", new_exec_rows, new_sets["new_market_top20"]),
        "old_watch_hot": ("old_radar", old_exec_rows, old_sets["old_watch_hot"]),
        "old_core_night_market_top20": ("old_radar", old_exec_rows, old_sets["old_core_night_market_top20"]),
    }
    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Execution node search on the three validated radar sets. Entry=next complete 15m Binance USDT-M open; all-taker 8bp; stop/protect first within each 15m bar.",
        "new_radar": {"meta": new_meta, "path_meta": new_path_meta, "sets": {}},
        "old_radar": {"meta": old_meta, "path_meta": old_path_meta, "sets": {}},
    }
    for name, (section, universe, rows) in selected_sets.items():
        block = _summarize_set(universe, rows, rules, sims=sims, seed=seed, set_name=name)
        block["best_by_cap5"] = _best_rules(block)
        result[section]["sets"][name] = block
    return result


def _format_rule_line(rule_name: str, block: Mapping[str, Any]) -> str:
    st = block["all_taker8"]
    cap5 = block["cap5"]
    cap10 = block["cap10"]
    rand = block.get("random_same_ts") or {}
    rand_p95 = rand.get("avg", {}).get("p95")
    rand_txt = "n/a" if rand_p95 is None else pct(rand_p95)
    reasons = ",".join(f"{k}:{v}" for k, v in block.get("exit_reasons", {}).items())
    return (
        f"{rule_name}: avg={pct(st['avg'])} win={st['win']*100:.1f}% sh={st['sharpe_like']:+.2f} comp={pct(st['comp'])} mdd={pct(st['mdd'])} "
        f"rand_p95_avg={rand_txt} pass={block.get('above_random_p95_avg')} "
        f"cap5={pct(cap5['comp'])}/sh{cap5['slot_stat']['sharpe_like']:+.2f} cap10={pct(cap10['comp'])}/sh{cap10['slot_stat']['sharpe_like']:+.2f} reasons={reasons}"
    )


def render_execution_node_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Radar Execution Node Search",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        str(result["method"]),
        "",
    ]
    for section in ["new_radar", "old_radar"]:
        lines.extend([f"## {section} input", "", "```text", json.dumps(result[section]["meta"], ensure_ascii=False, indent=2), json.dumps(result[section]["path_meta"], ensure_ascii=False, indent=2), "```", ""])
        for set_name, set_block in result[section]["sets"].items():
            lines.extend([f"## {set_name}", "", "```text", f"rows={set_block['rows']} symbols={set_block['symbols']}"])
            lines.append("best_by_cap5=" + json.dumps(set_block["best_by_cap5"], ensure_ascii=False, default=str))
            for rule_name, rule_block in set_block["rules"].items():
                lines.append(_format_rule_line(rule_name, rule_block))
            # top symbols for best rule
            best_rule = set_block["best_by_cap5"][0]["rule"] if set_block["best_by_cap5"] else None
            if best_rule:
                tops = set_block["rules"][best_rule]["top_symbol"]["top_symbols"][:8]
                lines.append("best_top_symbols=" + ", ".join(f"{x['symbol']} {pct(x['sum'])}/{x['n']}" for x in tops))
            lines.extend(["```", ""])
    return "\n".join(lines)


def write_execution_node_outputs(result: Mapping[str, Any], out_prefix: str | Path) -> tuple[Path, Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(render_execution_node_report(result))
    return json_path, md_path
