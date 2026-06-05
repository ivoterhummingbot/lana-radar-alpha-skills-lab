from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from .config import DEFAULT_SOURCE, OUTPUT_DIR, SourceConfig
from .signal_control import (
    COSTS,
    _group_by_ts,
    _stats_for_rows,
    _top_symbol_removal,
    attach_managed_1h,
    cap_portfolio,
    fmt_stat,
    iso,
    load_snapshot_rows,
    pct,
    stat,
)


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    score_field: str
    top_fraction: float
    gate_field: str | None = None
    gate_values: tuple[str, ...] = ()
    ascending: bool = False


def bjt_session(hour_bjt: int) -> str:
    if hour_bjt >= 20 or hour_bjt < 4:
        return "core_night"
    if 4 <= hour_bjt < 8:
        return "garbage_window"
    if 8 <= hour_bjt < 15:
        return "day_high_threshold"
    return "prewarm"


def _score_value(row: Mapping[str, Any], field: str) -> float:
    try:
        value = float(row.get(field) or 0.0)
    except Exception:
        value = 0.0
    if not math.isfinite(value):
        return 0.0
    return value


def top_fraction_per_timestamp(
    rows: Sequence[Mapping[str, Any]],
    score_field: str,
    top_fraction: float,
    *,
    ascending: bool = False,
) -> list[dict[str, Any]]:
    if top_fraction <= 0 or top_fraction > 1:
        raise ValueError("top_fraction must be in (0, 1]")
    selected: list[dict[str, Any]] = []
    by_ts = _group_by_ts(rows)
    for ts in sorted(by_ts):
        group = by_ts[ts]
        n = max(1, math.ceil(len(group) * top_fraction))
        selected.extend(
            sorted(
                group,
                key=lambda r: (
                    _score_value(r, score_field) if ascending else -_score_value(r, score_field),
                    str(r.get("symbol") or ""),
                ),
            )[:n]
        )
    return selected


def _row_gate_value(row: Mapping[str, Any], field: str | None) -> str | None:
    if field is None:
        return None
    if field == "session":
        return str(row.get("session") or bjt_session(int(row.get("hour_bjt") or 0)))
    return str(row.get(field))


def candidate_mask(rows: Sequence[Mapping[str, Any]], spec: CandidateSpec) -> list[dict[str, Any]]:
    gated = []
    for row in rows:
        if spec.gate_field is not None:
            if _row_gate_value(row, spec.gate_field) not in set(spec.gate_values):
                continue
        gated.append(dict(row))
    return top_fraction_per_timestamp(gated, spec.score_field, spec.top_fraction, ascending=spec.ascending)


def describe_candidate(spec: CandidateSpec) -> str:
    direction = "bottom" if spec.ascending else "top"
    frac = int(round(spec.top_fraction * 100))
    parts = [f"{direction}{frac} by {spec.score_field}"]
    if spec.gate_field:
        parts.append(f"{spec.gate_field} in {','.join(spec.gate_values)}")
    return "; ".join(parts)


def enrich_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    positive = [
        "maker_attention_score",
        "community_heat_score",
        "market_confirmation_score",
        "momentum_confirmation_score",
        "freshness_score",
        "source_quality_score",
        "attention_spread_score",
        "prev5m_confirmation_score",
    ]
    negative = ["warning_score", "fomo_risk_score", "prev5m_upper_wick_ratio"]
    for row0 in rows:
        row = dict(row0)
        row["session"] = bjt_session(int(row.get("hour_bjt") or 0))
        pos = mean(_score_value(row, f) for f in positive)
        neg = mean(_score_value(row, f) for f in negative)
        row["fixed_attention_composite"] = pos - neg
        row["low_risk_score"] = -mean(_score_value(row, f) for f in negative)
        out.append(row)
    return out


def generate_candidate_specs(rows: Sequence[Mapping[str, Any]]) -> list[CandidateSpec]:
    fractions = [0.10, 0.20, 0.33]
    high_fields = [
        "fixed_attention_composite",
        "unified_discovery_score",
        "maker_attention_score",
        "community_heat_score",
        "market_confirmation_score",
        "momentum_confirmation_score",
        "prev5m_confirmation_score",
        "source_quality_score",
        "freshness_score",
        "attention_spread_score",
        "symbol_rel5_vs_btc",
        "prev5m_ret",
        "prev5m_volume_ratio",
    ]
    low_fields = ["warning_score", "fomo_risk_score", "prev5m_upper_wick_ratio"]
    gates: list[tuple[str | None, tuple[str, ...], str]] = [
        (None, (), "all"),
        ("btc_gate_permission", ("allow",), "btc_allow"),
        ("session", ("core_night",), "core_night"),
        ("session", ("prewarm",), "prewarm"),
        ("session", ("core_night", "prewarm"), "active_session"),
        ("btc_gate_permission", ("allow", "None", "none"), "not_btc_deny"),
    ]
    specs: list[CandidateSpec] = []
    for gate_field, gate_values, gate_name in gates:
        for frac in fractions:
            pct_name = int(round(frac * 100))
            for field in high_fields:
                specs.append(
                    CandidateSpec(
                        name=f"{gate_name}__top{pct_name}__{field}",
                        score_field=field,
                        top_fraction=frac,
                        gate_field=gate_field,
                        gate_values=gate_values,
                    )
                )
            for field in low_fields:
                specs.append(
                    CandidateSpec(
                        name=f"{gate_name}__bottom{pct_name}__{field}",
                        score_field=field,
                        top_fraction=frac,
                        gate_field=gate_field,
                        gate_values=gate_values,
                        ascending=True,
                    )
                )
    return specs


def matched_random_distribution(
    universe_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    sims: int,
    seed: int,
) -> dict[str, Any]:
    universe_by_ts = _group_by_ts(universe_rows)
    counts = {ts: len(group) for ts, group in _group_by_ts(candidate_rows).items()}
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
            if n >= len(pool):
                picked.extend(pool)
            else:
                picked.extend(rng.sample(pool, n))
        gross = _stats_for_rows(picked, 0.0)
        avg_values.append(gross["avg"])
        sum_values.append(gross["sum"])
        cap5_values.append(cap_portfolio(picked, "managed_1h", COSTS["all_taker_8bp_total"], 5)["comp"])

    def qs(values: list[float]) -> dict[str, float]:
        xs = sorted(values)
        if not xs:
            return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
        def q(p: float) -> float:
            return xs[min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))]
        return {"p50": q(0.50), "p90": q(0.90), "p95": q(0.95), "p99": q(0.99)}

    return {"sims": sims, "avg": qs(avg_values), "sum": qs(sum_values), "cap5_all_taker_comp": qs(cap5_values)}


def evaluate_candidate(
    universe_rows: Sequence[Mapping[str, Any]],
    spec: CandidateSpec,
    *,
    sims: int,
    seed: int,
    min_rows: int = 30,
) -> dict[str, Any] | None:
    rows = candidate_mask(universe_rows, spec)
    rows = sorted(rows, key=lambda r: (r["ts_dt"], str(r.get("symbol") or "")))
    if len(rows) < min_rows:
        return None
    stats = {name: _stats_for_rows(rows, cost) for name, cost in COSTS.items()}
    capacity = {
        cost_name: {f"cap{cap}": cap_portfolio(rows, "managed_1h", cost, cap) for cap in [5, 10, 20]}
        for cost_name, cost in COSTS.items()
        if cost_name in {"gross_or_maker0", "one_taker_4bp_total", "all_taker_8bp_total"}
    }
    rand = matched_random_distribution(universe_rows, rows, sims=sims, seed=seed)
    top_removal = _top_symbol_removal(rows, COSTS["gross_or_maker0"])
    top5_stat = top_removal.get("remove_top5_symbols", {}).get("stat", stat([]))
    all_taker = stats["all_taker_8bp_total"]
    gross = stats["gross_or_maker0"]
    cap5_all_taker = capacity["all_taker_8bp_total"]["cap5"]
    pass_flags = {
        "gross_avg_above_random_p95": gross["avg"] > rand["avg"]["p95"],
        "gross_sum_above_random_p95": gross["sum"] > rand["sum"]["p95"],
        "all_taker_avg_positive": all_taker["avg"] > 0,
        "all_taker_cap5_positive": cap5_all_taker["comp"] > 0,
        "remove_top5_gross_avg_positive": top5_stat["avg"] > 0,
        "min_rows": len(rows) >= min_rows,
    }
    alpha_pass = all(pass_flags.values())
    robustness_score = sum(1 for ok in pass_flags.values() if ok)
    sort_score = (
        (10 if alpha_pass else 0)
        + robustness_score
        + gross["avg"] * 1000
        + all_taker["avg"] * 1000
        + cap5_all_taker["comp"] * 10
    )
    return {
        "name": spec.name,
        "description": describe_candidate(spec),
        "spec": spec.__dict__,
        "rows": len(rows),
        "symbols": len({str(r.get("symbol")) for r in rows}),
        "stats": stats,
        "capacity": capacity,
        "random_distribution": rand,
        "top_symbol_removal": top_removal,
        "pass_flags": pass_flags,
        "alpha_pass": alpha_pass,
        "robustness_score": robustness_score,
        "sort_score": sort_score,
    }


def run_score_regime_audit(
    source: SourceConfig = DEFAULT_SOURCE,
    *,
    hours: float | None = 24.0,
    sims: int = 300,
    seed: int = 20260601,
    top_n: int = 30,
) -> dict[str, Any]:
    snapshot_rows, load_meta = load_snapshot_rows(source=source, hours=hours)
    path_rows, path_meta = attach_managed_1h(snapshot_rows)
    universe = enrich_rows(path_rows)
    specs = generate_candidate_specs(universe)
    results: list[dict[str, Any]] = []
    for i, spec in enumerate(specs):
        evaluated = evaluate_candidate(universe, spec, sims=sims, seed=seed + i)
        if evaluated is not None:
            results.append(evaluated)
    results.sort(key=lambda r: (r["alpha_pass"], r["sort_score"]), reverse=True)
    return {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Score-ranking and regime candidate search with AlphaGBM-style same-timestamp random validation",
        "load_meta": load_meta,
        "path_meta": path_meta,
        "candidate_count": len(results),
        "alpha_pass_count": sum(1 for r in results if r["alpha_pass"]),
        "top_candidates": results[:top_n],
        "all_alpha_pass": [r for r in results if r["alpha_pass"]],
    }


def _short_candidate_line(candidate: Mapping[str, Any]) -> str:
    gross = candidate["stats"]["gross_or_maker0"]
    taker = candidate["stats"]["all_taker_8bp_total"]
    rand = candidate["random_distribution"]
    cap5 = candidate["capacity"]["all_taker_8bp_total"]["cap5"]
    return (
        f"{candidate['name']} rows={candidate['rows']} symbols={candidate['symbols']} "
        f"gross_avg={pct(gross['avg'])} taker8_avg={pct(taker['avg'])} "
        f"rand_p95_avg={pct(rand['avg']['p95'])} cap5_taker8={pct(cap5['comp'])} "
        f"flags={candidate['pass_flags']}"
    )


def render_score_regime_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Score Ranking + Regime Alpha Search",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "## Input",
        "",
        "```text",
        json.dumps({"load_meta": result["load_meta"], "path_meta": result["path_meta"]}, ensure_ascii=False, indent=2)[:3000],
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
    if result["all_alpha_pass"]:
        for cand in result["all_alpha_pass"][:30]:
            lines.append(_short_candidate_line(cand))
    else:
        lines.append("none")
    lines.extend(["```", "", "## Top candidates", "", "```text"])
    for cand in result["top_candidates"][:30]:
        lines.append(_short_candidate_line(cand))
    lines.extend(["```", ""])

    lines.append("## Top candidate details")
    for cand in result["top_candidates"][:10]:
        lines.extend([
            "",
            f"### {cand['name']}",
            cand["description"],
            "",
            "```text",
            f"gross:      {fmt_stat(cand['stats']['gross_or_maker0'])}",
            f"one_taker:  {fmt_stat(cand['stats']['one_taker_4bp_total'])}",
            f"all_taker:  {fmt_stat(cand['stats']['all_taker_8bp_total'])}",
            f"random_avg_p95={pct(cand['random_distribution']['avg']['p95'])}",
            f"random_sum_p95={pct(cand['random_distribution']['sum']['p95'])}",
            f"pass_flags={cand['pass_flags']}",
        ])
        for cost in ["gross_or_maker0", "one_taker_4bp_total", "all_taker_8bp_total"]:
            for cap_name, cap in cand["capacity"][cost].items():
                lines.append(f"{cost:<24} {cap_name:<5} comp={pct(cap['comp'])} mdd={pct(cap['mdd'])} taken={cap['taken']}")
        tr = cand["top_symbol_removal"]
        top = ", ".join(f"{r['symbol']} {pct(r['sum'])}/{r['n']}" for r in tr.get("top_symbols", [])[:8])
        lines.append("top_symbols: " + top)
        for key in ["remove_top1_symbols", "remove_top3_symbols", "remove_top5_symbols", "remove_top10_symbols"]:
            lines.append(f"{key:<22} {fmt_stat(tr.get(key, {}).get('stat', {}))}")
        lines.append("```")
    return "\n".join(lines)


def write_score_regime_outputs(result: Mapping[str, Any], out_prefix: Path) -> tuple[Path, Path]:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    md_path.write_text(render_score_regime_report(result))
    return json_path, md_path
