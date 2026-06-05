from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import OUTPUT_DIR
from .score_regime import enrich_rows, matched_random_distribution, top_fraction_per_timestamp
from .signal_control import (
    COSTS,
    _stats_for_rows,
    _top_symbol_removal,
    build_signal_and_controls,
    cap_portfolio,
    iso,
    load_snapshot_rows,
    attach_managed_1h,
    pct,
)


@dataclass(frozen=True)
class ShadowSet:
    name: str
    description: str
    rows: list[dict[str, Any]]
    cap: int
    rank_key: str = "unified_discovery_score"


def _ensure_ts_dt(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in rows:
        row = dict(src)
        if "ts_dt" not in row:
            ts = row.get("ts")
            if isinstance(ts, datetime):
                dt = ts
            else:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            row["ts_dt"] = dt.astimezone(timezone.utc)
        out.append(row)
    return out


def build_four_shadow_sets(rows: Sequence[Mapping[str, Any]]) -> dict[str, ShadowSet]:
    """Build the frozen four-shadow comparison set from already-scored rows."""

    prepared = enrich_rows(_ensure_ts_dt(rows))
    signal_sets = build_signal_and_controls(prepared, seed=7)
    old_rows = signal_sets["signal_not_momentum_cooldown1h"]
    core = [r for r in prepared if r.get("session") == "core_night"]
    new_a = top_fraction_per_timestamp(core, "momentum_confirmation_score", 0.20)
    new_b = top_fraction_per_timestamp(core, "market_confirmation_score", 0.10)
    return {
        "old_shadow_1_not_momentum_cap5": ShadowSet(
            name="old_shadow_1_not_momentum_cap5",
            description="new_radar + not_momentum_prev5m + managed_1h + score-ranked cap5",
            rows=old_rows,
            cap=5,
        ),
        "old_shadow_2_not_momentum_cap20": ShadowSet(
            name="old_shadow_2_not_momentum_cap20",
            description="new_radar + not_momentum_prev5m + managed_1h + score-ranked cap20",
            rows=old_rows,
            cap=20,
        ),
        "new_shadow_A_core_top20_momentum_cap5": ShadowSet(
            name="new_shadow_A_core_top20_momentum_cap5",
            description="core_night + top20 momentum_confirmation_score + managed_1h + cap5",
            rows=new_a,
            cap=5,
            rank_key="momentum_confirmation_score",
        ),
        "new_shadow_B_core_top10_market_cap5": ShadowSet(
            name="new_shadow_B_core_top10_market_cap5",
            description="core_night + top10 market_confirmation_score + managed_1h + cap5",
            rows=new_b,
            cap=5,
            rank_key="market_confirmation_score",
        ),
    }


def summarize_shadow(
    shadow: ShadowSet,
    universe_rows: Sequence[Mapping[str, Any]],
    *,
    sims: int,
    seed: int,
) -> dict[str, Any]:
    universe = enrich_rows(_ensure_ts_dt(universe_rows))
    rows = sorted(_ensure_ts_dt(shadow.rows), key=lambda r: (r["ts_dt"], str(r.get("symbol") or "")))
    gross = _stats_for_rows(rows, COSTS["gross_or_maker0"])
    one_taker = _stats_for_rows(rows, COSTS["one_taker_4bp_total"])
    all_taker = _stats_for_rows(rows, COSTS["all_taker_8bp_total"])
    capacity = {
        f"cap{cap}": cap_portfolio(rows, "managed_1h", COSTS["all_taker_8bp_total"], cap, rank_key=shadow.rank_key)
        for cap in [5, 10, 20]
    }
    random_p95 = matched_random_distribution(universe, rows, sims=sims, seed=seed)
    concentration = _top_symbol_removal(rows, COSTS["gross_or_maker0"])
    return {
        "name": shadow.name,
        "description": shadow.description,
        "cap": shadow.cap,
        "rank_key": shadow.rank_key,
        "rows": len(rows),
        "symbols": len({str(r.get("symbol")) for r in rows}),
        "gross": gross,
        "one_taker4bp": one_taker,
        "all_taker8": all_taker,
        "random_p95": random_p95,
        "pass_random_avg_p95": gross["avg"] > random_p95["avg"]["p95"],
        "pass_random_sum_p95": gross["sum"] > random_p95["sum"]["p95"],
        "capacity": capacity,
        "primary_capacity": capacity[f"cap{shadow.cap}"],
        "concentration": concentration,
    }


def compare_four_shadows_all_snapshots(*, sims: int = 300) -> dict[str, Any]:
    snapshots, load_meta = load_snapshot_rows(hours=0)
    path_rows, path_meta = attach_managed_1h(snapshots)
    path_rows = enrich_rows(path_rows)
    shadows = build_four_shadow_sets(path_rows)
    summaries = [
        summarize_shadow(shadow, path_rows, sims=sims, seed=20260601 + idx)
        for idx, shadow in enumerate(shadows.values())
    ]
    ranking = sorted(
        summaries,
        key=lambda s: (
            s["primary_capacity"]["comp"],
            s["all_taker8"]["avg"],
            s["pass_random_avg_p95"],
        ),
        reverse=True,
    )
    return {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "four shadow comparison over all available new-radar snapshot rows with completed managed_1h paths",
        "load_meta": load_meta,
        "path_meta": path_meta,
        "summaries": summaries,
        "ranking_by_primary_cap_comp": [s["name"] for s in ranking],
    }


def _stat_line(label: str, st: Mapping[str, Any]) -> str:
    return (
        f"{label:12s} n={st['n']:<5d} sum={pct(st['sum'])} avg={pct(st['avg'])} "
        f"med={pct(st['median'])} win={st['win']*100:5.1f}% sh={st['sharpe_like']:+.2f} "
        f"mdd={pct(st['mdd'])} comp={pct(st['comp'])}"
    )


def render_four_shadow_report(result: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "# Four Shadow Comparison — All New-Radar Snapshots",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "## Input",
        "",
        "```text",
        json.dumps({"load_meta": result["load_meta"], "path_meta": result["path_meta"]}, ensure_ascii=False, indent=2)[:3000],
        "```",
        "",
        "## Ranking by primary capacity comp",
        "",
        "```text",
        *[f"{i+1}. {name}" for i, name in enumerate(result["ranking_by_primary_cap_comp"])],
        "```",
        "",
        "## Shadow details",
    ]
    for s in result["summaries"]:
        cap_key = f"cap{s['cap']}"
        primary = s["primary_capacity"]
        cap10 = s["capacity"].get("cap10", {})
        rem = s["concentration"]
        top_symbols = rem.get("top_symbols", [])[:8]
        top5 = rem.get("remove_top5_symbols", {}).get("stat", {})
        lines += [
            "",
            f"### {s['name']}",
            s["description"],
            "",
            "```text",
            f"rows={s['rows']} symbols={s['symbols']} cap={s['cap']} rank_key={s['rank_key']}",
            _stat_line("gross", s["gross"]),
            _stat_line("one_taker", s["one_taker4bp"]),
            _stat_line("all_taker", s["all_taker8"]),
            f"random_p95_avg={pct(s['random_p95']['avg']['p95'])} random_p95_sum={pct(s['random_p95']['sum']['p95'])} pass_avg={s['pass_random_avg_p95']} pass_sum={s['pass_random_sum_p95']}",
            f"{cap_key}_all_taker comp={pct(primary['comp'])} mdd={pct(primary['mdd'])} taken={primary['taken']} skipped={primary['skipped']}",
            f"cap10_all_taker comp={pct(cap10.get('comp', 0.0))} mdd={pct(cap10.get('mdd', 0.0))} taken={cap10.get('taken', 0)} skipped={cap10.get('skipped', 0)}",
            f"remove_top5_gross avg={pct(top5.get('avg', 0.0))} sum={pct(top5.get('sum', 0.0))}",
            "top_symbols: " + ", ".join(
                f"{x['symbol']} {pct(x['sum'])}/{x['n']}" for x in top_symbols
            ),
            "```",
        ]
    return "\n".join(lines) + "\n"


def write_four_shadow_outputs(result: Mapping[str, Any], out_prefix: str | Path) -> tuple[Path, Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(render_four_shadow_report(result))
    return json_path, md_path
