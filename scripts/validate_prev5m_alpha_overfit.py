#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from spike_prev5m_alpha_search import (  # noqa: E402
    BJ,
    COST,
    Spec,
    apply_spec,
    new_market_top20,
    pct,
    stats_for,
    window_defs,
)
from radar_alpha_skills_lab.signal_control import (  # noqa: E402
    attach_managed_1h,
    cap_portfolio,
    load_snapshot_rows,
    stat,
)
from radar_alpha_skills_lab.score_regime import enrich_rows  # noqa: E402


@dataclass(frozen=True)
class Variant:
    name: str
    session: str = "night_20_04"
    rank_field: str = "symbol_rel5_vs_btc"
    fraction: float = 0.33
    rel5_min: float | None = 0.002
    wick_max: float | None = None
    fomo_max: float | None = None
    warning_max: float | None = None

    def to_spec(self) -> Spec:
        return Spec(
            self.name,
            self.session,
            self.rank_field,
            self.fraction,
            rel5_min=self.rel5_min,
            wick_max=self.wick_max,
            fomo_max=self.fomo_max,
            warning_max=self.warning_max,
        )


def q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))]


def random_same_ts(universe: Sequence[Mapping[str, Any]], cand: Sequence[Mapping[str, Any]], *, sims: int, seed: int) -> dict[str, Any]:
    by_u: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    by_c: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for r in universe:
        by_u[r["ts_dt"]].append(dict(r))
    for r in cand:
        by_c[r["ts_dt"]].append(dict(r))
    rng = random.Random(seed)
    avgs: list[float] = []
    sums: list[float] = []
    sharpes: list[float] = []
    for _ in range(sims):
        vals: list[float] = []
        for ts, group in by_c.items():
            pool = by_u.get(ts, [])
            if not pool:
                continue
            sample = rng.sample(pool, min(len(pool), len(group)))
            vals.extend(float(r["managed_1h"]) - COST for r in sample)
        st = stat(vals)
        avgs.append(float(st["avg"]))
        sums.append(float(st["sum"]))
        sharpes.append(float(st["sharpe_like"]))
    return {
        "avg": {"p50": q(avgs, 0.50), "p90": q(avgs, 0.90), "p95": q(avgs, 0.95), "p99": q(avgs, 0.99)},
        "sum": {"p50": q(sums, 0.50), "p90": q(sums, 0.90), "p95": q(sums, 0.95), "p99": q(sums, 0.99)},
        "sharpe_like": {"p50": q(sharpes, 0.50), "p90": q(sharpes, 0.90), "p95": q(sharpes, 0.95), "p99": q(sharpes, 0.99)},
    }


def contribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_sym: defaultdict[str, list[float]] = defaultdict(list)
    by_date: defaultdict[str, list[float]] = defaultdict(list)
    by_hour: defaultdict[int, list[float]] = defaultdict(list)
    by_reason: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = float(r["managed_1h"]) - COST
        by_sym[str(r.get("symbol"))].append(v)
        by_date[str(r.get("date_bjt"))].append(v)
        by_hour[int(r.get("hour_bjt") or 0)].append(v)
        by_reason[str(r.get("managed_1h_reason"))].append(v)
    ranked = sorted(((s, sum(vals), len(vals)) for s, vals in by_sym.items()), key=lambda x: x[1], reverse=True)
    out: dict[str, Any] = {
        "top_symbols": ranked[:12],
        "bottom_symbols": sorted(((s, sum(vals), len(vals)) for s, vals in by_sym.items()), key=lambda x: x[1])[:10],
        "by_date": {k: stat(v) for k, v in sorted(by_date.items())},
        "by_hour": {str(k): stat(v) for k, v in sorted(by_hour.items())},
        "by_reason": {k: stat(v) for k, v in sorted(by_reason.items())},
    }
    for n in [1, 3, 5, 10]:
        rem = {s for s, _v, _n in ranked[:n]}
        out[f"remove_top{n}"] = stat(float(r["managed_1h"]) - COST for r in rows if str(r.get("symbol")) not in rem)
    return out


def base_sets(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    a = new_market_top20(rows)
    night = [dict(r) for r in a if int(r.get("hour_bjt") or 0) >= 20 or int(r.get("hour_bjt") or 0) < 4]
    core = [dict(r) for r in a if int(r.get("hour_bjt") or 0) >= 20 or int(r.get("hour_bjt") or 0) < 8]
    return {"A_all": a, "A_night": night, "A_core": core}


def summarize_selected(selected: list[dict[str, Any]], universe: list[dict[str, Any]], *, sims: int, seed: int) -> dict[str, Any]:
    st = stats_for(selected)
    rand = random_same_ts(universe, selected, sims=sims, seed=seed) if selected else {}
    c = contribution(selected)
    return {
        "n": len(selected),
        "stats": st,
        "cap5": cap_portfolio(selected, "managed_1h", COST, 5),
        "cap10": cap_portfolio(selected, "managed_1h", COST, 10),
        "random_same_ts": rand,
        "contribution": c,
        "flags": {
            "avg_gt_rand_p95": bool(selected and st["taker"]["avg"] > rand["avg"]["p95"]),
            "sum_gt_rand_p95": bool(selected and st["taker"]["sum"] > rand["sum"]["p95"]),
            "sh_gt_rand_p95": bool(selected and st["taker"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
            "cap5_pos": bool(selected and cap_portfolio(selected, "managed_1h", COST, 5)["comp"] > 0),
            "remove_top5_avg_pos": bool(selected and c["remove_top5"]["avg"] > 0),
        },
    }


def fmt_line(label: str, block: Mapping[str, Any]) -> str:
    if block.get("n", 0) == 0:
        return f"{label:<32} n=0"
    t = block["stats"]["taker"]
    rand = block.get("random_same_ts") or {}
    return (
        f"{label:<32} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} "
        f"med={pct(t['median']):>8} win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} "
        f"mdd={pct(t['mdd']):>8} cap5={pct(block['cap5']['comp']):>8} cap10={pct(block['cap10']['comp']):>8} "
        f"rand95_avg={pct(rand.get('avg',{}).get('p95')):>8} rand95_sh={rand.get('sharpe_like',{}).get('p95', float('nan')):+5.2f} "
        f"remT5avg={pct(block['contribution']['remove_top5']['avg']):>8} flags={block['flags']}"
    )


def load_windows() -> tuple[list[tuple[str, datetime]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    _probe, latest_meta = load_snapshot_rows(hours=1)
    latest_complete = datetime.fromisoformat(latest_meta["complete_end_utc"])
    windows = window_defs(latest_complete)
    rows_by_window: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, Any] = {"latest_complete_utc": latest_complete.isoformat(), "windows": {}}
    for wname, end in windows:
        rows0, load_meta = load_snapshot_rows(hours=24, end_utc=end, now_utc=datetime.now(timezone.utc) + timedelta(days=1))
        path_rows, path_meta = attach_managed_1h(rows0)
        rows = enrich_rows([r for r in path_rows if r.get("managed_1h") is not None])
        rows_by_window[wname] = rows
        meta["windows"][wname] = {"end_utc": end.isoformat(), "load": load_meta, "path": path_meta, "rows": len(rows)}
    return windows, rows_by_window, meta


def main() -> int:
    sims = 500
    variants = [
        Variant("TARGET_R6_rel002_top33"),
        Variant("neighbor_rel000_top33", rel5_min=0.000),
        Variant("neighbor_rel001_top33", rel5_min=0.001),
        Variant("neighbor_rel003_top33", rel5_min=0.003),
        Variant("neighbor_rel005_top33", rel5_min=0.005),
        Variant("neighbor_rel002_top20", fraction=0.20),
        Variant("neighbor_rel002_top50", fraction=0.50),
        Variant("neighbor_rel002_top33_wick50", wick_max=0.50),
        Variant("neighbor_rel002_top33_risk70", fomo_max=70, warning_max=70),
        Variant("placebo_prev5ret_rel002_top33", rank_field="prev5m_ret", rel5_min=0.002, fraction=0.33),
    ]
    windows, rows_by_window, meta = load_windows()
    result: dict[str, Any] = {"generated_utc": datetime.now(timezone.utc).isoformat(), "sims": sims, "meta": meta, "fixed_rule": asdict(variants[0]), "windows": {}, "pooled": {}}
    lines: list[str] = [
        "# A_night_rel5_v1 overfit validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"latest_complete_utc: `{meta['latest_complete_utc']}`",
        "",
        "Fixed target rule: A=new_market_top20 ∩ BJT 20-04 ∩ symbol_rel5_vs_btc>=+0.2% ∩ top33% by symbol_rel5_vs_btc. No winner re-selection in this report.",
        "",
        "## Window-level fixed-rule + neighborhood",
        "```text",
    ]

    pooled_rows: dict[str, list[dict[str, Any]]] = {v.name: [] for v in variants}
    pooled_universe: list[dict[str, Any]] = []
    pooled_a_night: list[dict[str, Any]] = []

    for wi, (wname, _end) in enumerate(windows):
        rows = rows_by_window[wname]
        bases = base_sets(rows)
        universe = bases["A_night"]
        pooled_universe.extend(universe)
        pooled_a_night.extend(universe)
        result["windows"][wname] = {"bases": {}, "variants": {}}
        # Baselines first.
        for bname, brows in [("BASE_A_night_all", bases["A_night"]), ("BASE_A_core_all", bases["A_core"]), ("BASE_A_all", bases["A_all"])] :
            result["windows"][wname]["bases"][bname] = summarize_selected(brows, bases["A_all"], sims=sims, seed=2026060400 + wi)
        lines.append(f"# {wname}")
        lines.append(fmt_line("BASE_A_night_all", result["windows"][wname]["bases"]["BASE_A_night_all"]))
        for vi, variant in enumerate(variants):
            selected = apply_spec(rows, variant.to_spec())
            selected = sorted(selected, key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
            pooled_rows[variant.name].extend(selected)
            block = summarize_selected(selected, universe, sims=sims, seed=2026060500 + wi * 101 + vi)
            result["windows"][wname]["variants"][variant.name] = block
            lines.append(fmt_line(variant.name, block))
        lines.append("")

    lines.append("```")
    lines.append("")
    lines.append("## Pooled across available rolling windows")
    lines.append("```text")
    # Pooled by exact rows may duplicate latest24 with day windows; treat as stability stress, not independent sample.
    for vi, variant in enumerate(variants):
        selected = sorted(pooled_rows[variant.name], key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
        block = summarize_selected(selected, pooled_universe, sims=sims, seed=2026060600 + vi)
        result["pooled"][variant.name] = block
        lines.append(fmt_line(variant.name, block))
    lines.append("```")

    # LODO on target: remove one BJT date across pooled rows.
    target = pooled_rows[variants[0].name]
    dates = sorted({str(r.get("date_bjt")) for r in target})
    lodo: dict[str, Any] = {}
    lines.append("")
    lines.append("## Target LODO / leave-one-BJT-date-out")
    lines.append("```text")
    for d in dates:
        sel = [r for r in target if str(r.get("date_bjt")) != d]
        uni = [r for r in pooled_universe if str(r.get("date_bjt")) != d]
        block = summarize_selected(sel, uni, sims=sims, seed=2026060700 + len(lodo))
        lodo[d] = block
        lines.append(fmt_line(f"leave_out_{d}", block))
    result["target_lodo"] = lodo
    lines.append("```")

    # Target detailed contribution.
    target_block = result["pooled"][variants[0].name]
    lines.append("")
    lines.append("## Target contribution diagnostics")
    lines.append("```text")
    lines.append("top_symbols=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in target_block["contribution"]["top_symbols"][:10]))
    lines.append("bottom_symbols=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in target_block["contribution"]["bottom_symbols"][:8]))
    for k in ["remove_top1", "remove_top3", "remove_top5", "remove_top10"]:
        st = target_block["contribution"][k]
        lines.append(f"{k}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])}")
    lines.append("by_date:")
    for d, st in target_block["contribution"]["by_date"].items():
        lines.append(f"  {d}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])}")
    lines.append("by_hour:")
    for h, st in target_block["contribution"]["by_hour"].items():
        lines.append(f"  {h}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])}")
    lines.append("by_exit_reason:")
    for reason, st in target_block["contribution"]["by_reason"].items():
        lines.append(f"  {reason}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f}")
    lines.append("```")

    out = PROJECT_ROOT / "output" / f"prev5m-alpha-overfit-validation-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
