from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import DEFAULT_SOURCE
from .score_regime import bjt_session
from .signal_control import COSTS, _top_symbol_removal, iso, pct, stat


HORIZON_HOURS = {"1h": 1.0, "4h": 4.0, "24h": 24.0}
TOKENIZED_STOCKS = {
    "AMD", "QCOM", "RKLB", "MU", "NVDA", "MSFT", "AAPL", "TSLA", "COIN", "MSTR",
    "QQQ", "SPY", "SOXL", "MRVL", "DRAM", "OPENAI", "XAU", "XAG",
}


@dataclass(frozen=True)
class CandidateRule:
    name: str
    horizon: str = "1h"
    gate_field: str | None = None
    gate_values: tuple[str, ...] = ()
    score_field: str | None = None
    top_fraction: float | None = None
    ascending: bool = False


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_symbol(symbol: str) -> str:
    s = str(symbol).upper().strip()
    if "/" in s:
        return s.split("/", 1)[0]
    return s[:-4] if s.endswith("USDT") else s


def _score(row: Mapping[str, Any], field: str) -> float:
    try:
        value = float(row.get(field) or 0.0)
    except Exception:
        return 0.0
    return value if math.isfinite(value) else 0.0


def _group_by_ts(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["ts_dt"]].append(dict(row))
    return grouped


def candidate_rows(rows: Sequence[Mapping[str, Any]], rule: CandidateRule) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    allowed = set(rule.gate_values)
    return_key = f"return_{rule.horizon}"
    for row0 in rows:
        row = dict(row0)
        if row.get("horizon") != rule.horizon and return_key not in row:
            continue
        if row.get(return_key) is None:
            continue
        if rule.gate_field is not None and str(row.get(rule.gate_field)) not in allowed:
            continue
        filtered.append(row)
    if not rule.score_field or not rule.top_fraction:
        return sorted(filtered, key=lambda r: (r["ts_dt"], str(r.get("symbol") or "")))
    if rule.top_fraction <= 0 or rule.top_fraction > 1:
        raise ValueError("top_fraction must be in (0, 1]")
    selected: list[dict[str, Any]] = []
    for ts, group in sorted(_group_by_ts(filtered).items()):
        n = max(1, math.ceil(len(group) * rule.top_fraction))
        selected.extend(
            sorted(
                group,
                key=lambda r: (
                    _score(r, rule.score_field or "") if rule.ascending else -_score(r, rule.score_field or ""),
                    str(r.get("symbol") or ""),
                ),
            )[:n]
        )
    return selected


def _values(rows: Sequence[Mapping[str, Any]], horizon: str, cost: float = 0.0) -> list[float]:
    key = f"return_{horizon}"
    return [float(r[key]) - cost for r in rows if r.get(key) is not None]


def _stats(rows: Sequence[Mapping[str, Any]], horizon: str, cost: float = 0.0) -> dict[str, Any]:
    return stat(_values(rows, horizon, cost))


def cap_portfolio_horizon(
    rows: Sequence[Mapping[str, Any]],
    horizon: str,
    *,
    cost: float,
    cap: int,
    rank_key: str = "final_score",
) -> dict[str, Any]:
    hours = HORIZON_HOURS[horizon]
    sortable: list[tuple[datetime, float, str, datetime, float]] = []
    key = f"return_{horizon}"
    for row in rows:
        if row.get(key) is None:
            continue
        ent = row["ts_dt"]
        pnl = float(row[key]) - cost
        sortable.append((ent, -_score(row, rank_key), str(row.get("symbol") or ""), ent + timedelta(hours=hours), pnl))
    sortable.sort(key=lambda x: (x[0], x[1], x[2]))
    active: list[tuple[datetime, str]] = []
    chosen: list[float] = []
    taken_symbols: list[str] = []
    skipped = 0
    for ent, _neg_rank, sym, exit_dt, pnl in sortable:
        active = [(ex, s) for ex, s in active if ex > ent]
        if len(active) >= cap:
            skipped += 1
            continue
        active.append((exit_dt, sym))
        chosen.append(pnl / cap)
        taken_symbols.append(sym)
    st = stat(chosen)
    return {"cap": cap, "taken": len(chosen), "skipped": skipped, "taken_symbols": taken_symbols, "slot_stat": st, "comp": st["comp"], "mdd": st["mdd"]}


def matched_random_distribution(
    universe_rows: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    horizon: str,
    *,
    sims: int,
    seed: int,
) -> dict[str, Any]:
    universe_by_ts = _group_by_ts(universe_rows)
    counts = {ts: len(group) for ts, group in _group_by_ts(candidate).items()}
    rng = random.Random(seed)
    avg_values: list[float] = []
    sum_values: list[float] = []
    cap5_values: list[float] = []
    for _ in range(sims):
        picked: list[dict[str, Any]] = []
        for ts, n in counts.items():
            pool = sorted(universe_by_ts.get(ts, []), key=lambda r: str(r.get("symbol") or ""))
            if not pool:
                continue
            picked.extend(pool if n >= len(pool) else rng.sample(pool, n))
        gross = _stats(picked, horizon, 0.0)
        avg_values.append(gross["avg"])
        sum_values.append(gross["sum"])
        cap5_values.append(cap_portfolio_horizon(picked, horizon, cost=COSTS["all_taker_8bp_total"], cap=5)["comp"])

    def qs(values: list[float]) -> dict[str, float]:
        xs = sorted(values)
        if not xs:
            return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
        def q(p: float) -> float:
            return xs[min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))]
        return {"p50": q(0.50), "p90": q(0.90), "p95": q(0.95), "p99": q(0.99)}

    return {"sims": sims, "avg": qs(avg_values), "sum": qs(sum_values), "cap5_all_taker_comp": qs(cap5_values)}


def _top_symbol_removal_for_horizon(rows: Sequence[Mapping[str, Any]], horizon: str, cost: float) -> dict[str, Any]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    key = f"return_{horizon}"
    for r in rows:
        by_sym[str(r.get("symbol"))].append(float(r[key]) - cost)
    ranked = sorted(((sym, sum(vals), len(vals)) for sym, vals in by_sym.items()), key=lambda x: x[1], reverse=True)
    result: dict[str, Any] = {"top_symbols": [{"symbol": s, "sum": v, "n": n} for s, v, n in ranked[:10]]}
    for k in [1, 3, 5, 10]:
        removed = {s for s, _v, _n in ranked[:k]}
        result[f"remove_top{k}_symbols"] = {
            "removed": sorted(removed),
            "stat": stat(float(r[key]) - cost for r in rows if str(r.get("symbol")) not in removed),
        }
    return result


def _positive_day_ratio(rows: Sequence[Mapping[str, Any]], horizon: str, cost: float) -> dict[str, Any]:
    by_day: dict[str, list[float]] = defaultdict(list)
    key = f"return_{horizon}"
    for r in rows:
        by_day[str(r.get("date_bjt"))].append(float(r[key]) - cost)
    sums = {day: sum(vals) for day, vals in by_day.items()}
    return {
        "days": len(sums),
        "positive_days": sum(1 for v in sums.values() if v > 0),
        "positive_day_ratio": (sum(1 for v in sums.values() if v > 0) / len(sums)) if sums else 0.0,
        "day_sums": dict(sorted(sums.items())),
    }


def summarize_old_candidate(
    universe_rows: Sequence[Mapping[str, Any]],
    rule: CandidateRule,
    *,
    sims: int,
    seed: int,
    min_rows: int = 30,
) -> dict[str, Any] | None:
    rows = candidate_rows(universe_rows, rule)
    if len(rows) < min_rows:
        return None
    horizon = rule.horizon
    rank_key = rule.score_field or "final_score"
    gross = _stats(rows, horizon, COSTS["gross_or_maker0"])
    all_taker = _stats(rows, horizon, COSTS["all_taker_8bp_total"])
    cap = {f"cap{c}": cap_portfolio_horizon(rows, horizon, cost=COSTS["all_taker_8bp_total"], cap=c, rank_key=rank_key) for c in [5, 10, 20]}
    rand = matched_random_distribution(universe_rows, rows, horizon, sims=sims, seed=seed)
    concentration = _top_symbol_removal_for_horizon(rows, horizon, COSTS["gross_or_maker0"])
    day = _positive_day_ratio(rows, horizon, COSTS["all_taker_8bp_total"])
    flags = {
        "gross_avg_above_random_p95": gross["avg"] > rand["avg"]["p95"],
        "gross_sum_above_random_p95": gross["sum"] > rand["sum"]["p95"],
        "all_taker_avg_positive": all_taker["avg"] > 0,
        "all_taker_cap5_positive": cap["cap5"]["comp"] > 0,
        "min_rows": len(rows) >= min_rows,
        "positive_day_ratio_ge_50": day["positive_day_ratio"] >= 0.5,
    }
    alpha_pass = all(flags.values())
    sort_score = (
        (10 if alpha_pass else 0)
        + sum(1 for v in flags.values() if v)
        + all_taker["avg"] * 1000
        + cap["cap5"]["comp"] * 10
        + gross["avg"] * 1000
    )
    return {
        "name": rule.name,
        "rule": rule.__dict__,
        "horizon": horizon,
        "rows": len(rows),
        "symbols": len({str(r.get("symbol")) for r in rows}),
        "gross": gross,
        "all_taker8": all_taker,
        "capacity": cap,
        "random_p95": rand,
        "concentration": concentration,
        "day": day,
        "pass_flags": flags,
        "alpha_pass": alpha_pass,
        "sort_score": sort_score,
    }


def _load_new_first_seen_by_symbol(source=DEFAULT_SOURCE) -> dict[str, datetime]:
    if not source.maker_attention_db.exists():
        return {}
    query = "select symbol, min(ts) as first_ts from maker_attn_symbol_scores group by symbol"
    out: dict[str, datetime] = {}
    with sqlite3.connect(f"file:{source.maker_attention_db}?mode=ro", uri=True) as con:
        for sym, ts in con.execute(query):
            if ts:
                out[clean_symbol(str(sym))] = parse_ts(str(ts))
    return out


def _delay_bucket(old_ts: datetime, new_first: datetime | None) -> str:
    if new_first is None:
        return "no_new_seen"
    mins = (old_ts - new_first).total_seconds() / 60.0
    if mins < 0:
        return "old_before_new"
    if mins <= 60:
        return "new_to_old_0_1h"
    if mins <= 4 * 60:
        return "new_to_old_1_4h"
    if mins <= 24 * 60:
        return "new_to_old_4_24h"
    return "new_to_old_gt24h"


def load_old_radar_rows(source=DEFAULT_SOURCE) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    new_first = _load_new_first_seen_by_symbol(source)
    query = """
        select o.ts, o.symbol, o.decision_status, o.horizon, o.close_return,
               o.mfe, o.mae,
               s.community_heat_score, s.market_confirmation_score, s.momentum_confirmation_score,
               s.momentum_stage, s.entry_trigger_score, s.entry_trigger_stage,
               s.entry_reject_reason, s.episode_quality_score, s.regime_score,
               s.final_score, s.reject_reason, s.recommended_action
        from community_forward_outcomes o
        join lana_community_scores s
          on o.ts=s.ts and o.symbol=s.symbol and o.decision_status=s.decision_status
        where o.horizon in ('1h','4h','24h')
        order by o.ts, o.symbol, o.horizon
    """
    wide: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_rows = 0
    with sqlite3.connect(f"file:{source.community_history_db}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for r0 in con.execute(query):
            raw_rows += 1
            r = dict(r0)
            sym = clean_symbol(str(r["symbol"]))
            if not sym or sym in TOKENIZED_STOCKS:
                continue
            ts_dt = parse_ts(str(r["ts"]))
            key = (iso(ts_dt), sym, str(r["decision_status"]))
            row = wide.setdefault(
                key,
                {
                    "ts": iso(ts_dt),
                    "ts_dt": ts_dt,
                    "symbol": sym,
                    "date_bjt": ts_dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                    "hour_bjt": ts_dt.astimezone(timezone(timedelta(hours=8))).hour,
                    "session": bjt_session(ts_dt.astimezone(timezone(timedelta(hours=8))).hour),
                    "decision_status": str(r["decision_status"]),
                    "recommended_action": str(r.get("recommended_action") or ""),
                    "momentum_stage": str(r.get("momentum_stage") or ""),
                    "entry_trigger_stage": str(r.get("entry_trigger_stage") or ""),
                    "eligible_long": "eligible" if str(r["decision_status"]) in {"watch_hot", "setup_candidate"} and not str(r.get("recommended_action") or "").startswith("avoid") else "not_eligible",
                    "status_session": f"{r['decision_status']}|{bjt_session(ts_dt.astimezone(timezone(timedelta(hours=8))).hour)}",
                    "action_session": f"{str(r.get('recommended_action') or '')}|{bjt_session(ts_dt.astimezone(timezone(timedelta(hours=8))).hour)}",
                    "delay_bucket": _delay_bucket(ts_dt, new_first.get(sym)),
                },
            )
            for f in [
                "community_heat_score", "market_confirmation_score", "momentum_confirmation_score",
                "entry_trigger_score", "episode_quality_score", "regime_score", "final_score",
            ]:
                row[f] = float(r.get(f) or 0.0)
            h = str(r["horizon"])
            row[f"return_{h}"] = float(r["close_return"])
            row[f"mfe_{h}"] = float(r["mfe"])
            row[f"mae_{h}"] = float(r["mae"])
    rows = list(wide.values())
    latest_ts = max((r["ts_dt"] for r in rows), default=None)
    complete_cutoffs: dict[str, str | None] = {}
    if latest_ts is not None:
        for h, hours in HORIZON_HOURS.items():
            cutoff = latest_ts - timedelta(hours=hours)
            complete_cutoffs[h] = iso(cutoff)
            for row in rows:
                if row["ts_dt"] > cutoff:
                    row[f"return_{h}"] = None
                    row[f"mfe_{h}"] = None
                    row[f"mae_{h}"] = None
    meta = {
        "source_db": str(source.community_history_db),
        "raw_outcome_rows": raw_rows,
        "wide_rows": len(rows),
        "min_ts": min((r["ts"] for r in rows), default=None),
        "max_ts": max((r["ts"] for r in rows), default=None),
        "complete_cutoffs": complete_cutoffs,
        "symbols": len({r["symbol"] for r in rows}),
    }
    return rows, meta


def generate_old_candidate_rules(rows: Sequence[Mapping[str, Any]]) -> list[CandidateRule]:
    """Generate a deliberately small no-refit search grid.

    The first exhaustive version was too slow for interactive use and also too
    easy to overfit. Keep the old-radar audit focused on interpretable gates:
    status/action/session/delay, plus top-score cuts only on broad gates.
    """
    horizons = ["1h", "4h", "24h"]
    score_fields = [
        "final_score", "community_heat_score", "market_confirmation_score", "momentum_confirmation_score",
        "entry_trigger_score", "episode_quality_score", "regime_score",
    ]
    fractions = [0.10, 0.20, 0.33]
    broad_gates: list[tuple[str | None, tuple[str, ...], str]] = [(None, (), "all")]
    simple_gates: list[tuple[str | None, tuple[str, ...], str]] = []
    for field in ["decision_status", "recommended_action", "session", "eligible_long", "delay_bucket"]:
        values = sorted({str(r.get(field)) for r in rows if r.get(field) not in (None, "")})
        for value in values:
            item = (field, (value,), f"{field}={value}")
            simple_gates.append(item)
            if field in {"decision_status", "session", "eligible_long", "delay_bucket"}:
                broad_gates.append(item)
    rules: list[CandidateRule] = []
    for h in horizons:
        for field, values, gate_name in simple_gates + [(None, (), "all")]:
            rules.append(CandidateRule(name=f"{h}__{gate_name}", horizon=h, gate_field=field, gate_values=values))
        for field, values, gate_name in broad_gates:
            for score_field in score_fields:
                for frac in fractions:
                    rules.append(CandidateRule(
                        name=f"{h}__{gate_name}__top{int(frac*100)}__{score_field}",
                        horizon=h,
                        gate_field=field,
                        gate_values=values,
                        score_field=score_field,
                        top_fraction=frac,
                    ))
    seen = set()
    unique = []
    for r in rules:
        if r.name not in seen:
            unique.append(r)
            seen.add(r.name)
    return unique


def run_old_radar_alpha_search(*, sims: int = 200, top_n: int = 50) -> dict[str, Any]:
    rows, meta = load_old_radar_rows()
    rules = generate_old_candidate_rules(rows)
    results: list[dict[str, Any]] = []
    for idx, rule in enumerate(rules):
        summary = summarize_old_candidate(rows, rule, sims=sims, seed=20260601 + idx)
        if summary is not None:
            results.append(summary)
    results.sort(key=lambda r: (r["alpha_pass"], r["sort_score"]), reverse=True)
    return {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "old Lana delayed-radar alpha search using community_forward_outcomes close-return horizons, all-taker 8bp, same-timestamp random validation and cap portfolio",
        "input_meta": meta,
        "candidate_count": len(results),
        "alpha_pass_count": sum(1 for r in results if r["alpha_pass"]),
        "top_candidates": results[:top_n],
        "all_alpha_pass": [r for r in results if r["alpha_pass"]],
    }


def _stat_line(label: str, st: Mapping[str, Any]) -> str:
    return (
        f"{label:10s} n={st['n']:<5d} sum={pct(st['sum'])} avg={pct(st['avg'])} med={pct(st['median'])} "
        f"win={st['win']*100:5.1f}% sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])} comp={pct(st['comp'])}"
    )


def _short_line(c: Mapping[str, Any]) -> str:
    cap5 = c["capacity"]["cap5"]
    return (
        f"{c['name']} rows={c['rows']} symbols={c['symbols']} "
        f"taker8_avg={pct(c['all_taker8']['avg'])} sh={c['all_taker8']['sharpe_like']:+.2f} "
        f"cap5={pct(cap5['comp'])} cap5_sh={cap5['slot_stat']['sharpe_like']:+.2f} "
        f"rand_p95_avg={pct(c['random_p95']['avg']['p95'])} flags={c['pass_flags']}"
    )


def render_old_radar_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Old Lana Delayed Radar Alpha Search",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "## Input",
        "",
        "```text",
        json.dumps(result["input_meta"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Summary",
        "",
        "```text",
        f"candidate_count={result['candidate_count']}",
        f"alpha_pass_count={result['alpha_pass_count']}",
        "```",
        "",
        "## Alpha-pass candidates",
        "",
        "```text",
    ]
    passes = result.get("all_alpha_pass") or []
    if not passes:
        lines.append("none")
    else:
        for c in passes[:50]:
            lines.append(_short_line(c))
    lines += ["```", "", "## Top candidates", "", "```text"]
    for c in result.get("top_candidates", [])[:30]:
        lines.append(_short_line(c))
    lines += ["```", "", "## Top candidate details"]
    for c in result.get("top_candidates", [])[:12]:
        cap5 = c["capacity"]["cap5"]
        cap10 = c["capacity"]["cap10"]
        top_symbols = c["concentration"].get("top_symbols", [])[:8]
        top5 = c["concentration"].get("remove_top5_symbols", {}).get("stat", {})
        lines += [
            "",
            f"### {c['name']}",
            "",
            "```text",
            f"rows={c['rows']} symbols={c['symbols']} horizon={c['horizon']}",
            _stat_line("gross", c["gross"]),
            _stat_line("all_taker", c["all_taker8"]),
            f"random_p95_avg={pct(c['random_p95']['avg']['p95'])} random_p95_sum={pct(c['random_p95']['sum']['p95'])}",
            f"cap5 all_taker comp={pct(cap5['comp'])} sh={cap5['slot_stat']['sharpe_like']:+.2f} mdd={pct(cap5['mdd'])} taken={cap5['taken']} skipped={cap5['skipped']}",
            f"cap10 all_taker comp={pct(cap10['comp'])} sh={cap10['slot_stat']['sharpe_like']:+.2f} mdd={pct(cap10['mdd'])} taken={cap10['taken']} skipped={cap10['skipped']}",
            f"positive_day_ratio={c['day']['positive_day_ratio']:.2f} days={c['day']['days']}",
            f"remove_top5 gross avg={pct(top5.get('avg', 0.0))} sum={pct(top5.get('sum', 0.0))}",
            "top_symbols: " + ", ".join(f"{x['symbol']} {pct(x['sum'])}/{x['n']}" for x in top_symbols),
            f"flags={c['pass_flags']}",
            "```",
        ]
    return "\n".join(lines) + "\n"


def write_old_radar_outputs(result: Mapping[str, Any], out_prefix: str | Path) -> tuple[Path, Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(render_old_radar_report(result))
    return json_path, md_path
