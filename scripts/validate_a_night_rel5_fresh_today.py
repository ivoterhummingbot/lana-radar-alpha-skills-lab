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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from spike_prev5m_alpha_search import COST, Spec, apply_spec, new_market_top20, pct, stats_for  # noqa: E402
from radar_alpha_skills_lab.signal_control import attach_managed_1h, cap_portfolio, load_snapshot_rows, stat  # noqa: E402
from radar_alpha_skills_lab.score_regime import enrich_rows  # noqa: E402

BJ = timezone(timedelta(hours=8))
FREEZE_UTC = datetime(2026, 6, 3, 4, 15, tzinfo=timezone.utc)  # overfit validation/final freeze; BJT 12:15
CONCLUSION_UTC = datetime(2026, 6, 3, 4, 3, tzinfo=timezone.utc)  # final conclusion file; BJT 12:03
TODAY_BJT_START_UTC = datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)  # BJT 2026-06-03 00:00
SIMS = 500


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


def night(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        h = int(r.get("hour_bjt") or 0)
        if h >= 20 or h < 4:
            out.append(dict(r))
    return out


def contribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_sym: defaultdict[str, list[float]] = defaultdict(list)
    by_hour: defaultdict[int, list[float]] = defaultdict(list)
    by_reason: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = float(r["managed_1h"]) - COST
        by_sym[str(r.get("symbol"))].append(v)
        by_hour[int(r.get("hour_bjt") or 0)].append(v)
        by_reason[str(r.get("managed_1h_reason"))].append(v)
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    d: dict[str, Any] = {
        "top_symbols": ranked[:12],
        "bottom_symbols": sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1])[:10],
        "by_hour": {str(k): stat(v) for k, v in sorted(by_hour.items())},
        "by_reason": {k: stat(v) for k, v in sorted(by_reason.items())},
    }
    for n in [1, 3, 5, 10]:
        rem = {s for s, _sum, _n in ranked[:n]}
        d[f"remove_top{n}"] = stat(float(r["managed_1h"]) - COST for r in rows if str(r.get("symbol")) not in rem)
    return d


def summarize(label: str, rows: list[dict[str, Any]], universe: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    if rows:
        st = stats_for(rows)
        rand = random_same_ts(universe, rows, sims=SIMS, seed=seed) if universe else {}
        cap5 = cap_portfolio(rows, "managed_1h", COST, 5)
        cap10 = cap_portfolio(rows, "managed_1h", COST, 10)
        contrib = contribution(rows)
        flags = {
            "avg_gt_rand_p95": bool(rand and st["taker"]["avg"] > rand["avg"]["p95"]),
            "sum_gt_rand_p95": bool(rand and st["taker"]["sum"] > rand["sum"]["p95"]),
            "sh_gt_rand_p95": bool(rand and st["taker"]["sharpe_like"] > rand["sharpe_like"]["p95"]),
            "cap5_pos": cap5["comp"] > 0,
            "cap10_pos": cap10["comp"] > 0,
            "remove_top5_avg_pos": contrib["remove_top5"]["avg"] > 0,
        }
    else:
        st, rand, cap5, cap10, contrib, flags = {}, {}, {}, {}, {}, {}
    return {"label": label, "n": len(rows), "stats": st, "random_same_ts": rand, "cap5": cap5, "cap10": cap10, "contribution": contrib, "flags": flags}


def fmt(block: Mapping[str, Any]) -> str:
    label = str(block["label"])
    if block["n"] == 0:
        return f"{label:<30} n=0"
    t = block["stats"]["taker"]
    rand = block["random_same_ts"]
    return (
        f"{label:<30} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} med={pct(t['median']):>8} "
        f"win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} mdd={pct(t['mdd']):>8} "
        f"cap5={pct(block['cap5']['comp']):>8} cap10={pct(block['cap10']['comp']):>8} "
        f"rand95_avg={pct(rand['avg']['p95']):>8} rand95_sum={pct(rand['sum']['p95']):>8} remT5avg={pct(block['contribution']['remove_top5']['avg']):>8} "
        f"flags={block['flags']}"
    )


def main() -> int:
    # Load enough rows from BJT today start to now; load_snapshot_rows enforces 1h+2m complete horizon.
    _probe, latest_meta = load_snapshot_rows(hours=1)
    complete_end = datetime.fromisoformat(latest_meta["complete_end_utc"])
    hours = (complete_end - TODAY_BJT_START_UTC).total_seconds() / 3600 + 0.2
    rows0, load_meta = load_snapshot_rows(hours=hours, end_utc=complete_end)
    rows_path, path_meta = attach_managed_1h(rows0)
    rows = enrich_rows([r for r in rows_path if r.get("managed_1h") is not None])

    spec = Spec("A_night_rel5_v1_shadow", "night_20_04", "symbol_rel5_vs_btc", 0.33, rel5_min=0.002)
    all_a_night = night(new_market_top20(rows))
    selected_today = apply_spec(rows, spec)
    selected_fresh_conclusion = [r for r in selected_today if r["ts_dt"] >= CONCLUSION_UTC]
    selected_fresh_strict = [r for r in selected_today if r["ts_dt"] >= FREEZE_UTC]
    universe_today = all_a_night
    universe_fresh_conclusion = [r for r in all_a_night if r["ts_dt"] >= CONCLUSION_UTC]
    universe_fresh_strict = [r for r in all_a_night if r["ts_dt"] >= FREEZE_UTC]

    blocks = {
        "today_bjt_00_now_all_night": summarize("today_bjt_00_now_all_night", selected_today, universe_today, 202606031001),
        "fresh_after_conclusion_1203": summarize("fresh_after_conclusion_1203", selected_fresh_conclusion, universe_fresh_conclusion, 202606031002),
        "fresh_after_overfit_1215": summarize("fresh_after_overfit_1215", selected_fresh_strict, universe_fresh_strict, 202606031003),
    }

    result = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "rule": {
            "name": "A_night_rel5_v1_shadow",
            "pool": "new_market_top20 = per timestamp top20% by market_confirmation_score",
            "window_bjt": "20:00-04:00",
            "filter": "symbol_rel5_vs_btc >= 0.002",
            "rank": "top33% per timestamp by symbol_rel5_vs_btc",
            "execution": path_meta.get("entry_model"),
            "cost": "all_taker_8bp_total",
        },
        "cutoffs": {
            "today_bjt_start_utc": TODAY_BJT_START_UTC.isoformat(),
            "conclusion_utc": CONCLUSION_UTC.isoformat(),
            "freeze_utc": FREEZE_UTC.isoformat(),
            "complete_end_utc": complete_end.isoformat(),
            "complete_end_bjt": complete_end.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        },
        "load_meta": load_meta,
        "path_meta": path_meta,
        "counts": {
            "rows_loaded": len(rows),
            "a_night_universe_today": len(universe_today),
            "a_night_universe_after_conclusion": len(universe_fresh_conclusion),
            "a_night_universe_after_freeze": len(universe_fresh_strict),
        },
        "blocks": blocks,
    }

    lines = [
        "# A_night_rel5_v1_shadow fresh-today validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"complete_end_bjt: `{result['cutoffs']['complete_end_bjt']}`",
        "",
        "Rule frozen: A_new_market_top20 ∩ BJT 20-04 ∩ symbol_rel5_vs_btc>=+0.2% ∩ top33% by symbol_rel5_vs_btc; next full 1m open + managed_1h; all-taker 8bp.",
        "",
        "Cutoffs:",
        f"- conclusion cutoff: `{CONCLUSION_UTC.isoformat()}` = BJT `{CONCLUSION_UTC.astimezone(BJ).strftime('%Y-%m-%d %H:%M')}`",
        f"- strict overfit/freeze cutoff: `{FREEZE_UTC.isoformat()}` = BJT `{FREEZE_UTC.astimezone(BJ).strftime('%Y-%m-%d %H:%M')}`",
        "",
        "## Summary",
        "```text",
    ]
    for k in ["today_bjt_00_now_all_night", "fresh_after_conclusion_1203", "fresh_after_overfit_1215"]:
        lines.append(fmt(blocks[k]))
    lines.extend(["```", ""])

    for k, block in blocks.items():
        lines.append(f"## {k}")
        if block["n"] == 0:
            lines.append("No eligible A_night_rel5_v1_shadow trades in this slice.")
            lines.append("")
            continue
        c = block["contribution"]
        lines.append("```text")
        lines.append("top_symbols=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in c["top_symbols"][:10]))
        lines.append("bottom_symbols=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in c["bottom_symbols"][:8]))
        for rem in ["remove_top1", "remove_top3", "remove_top5", "remove_top10"]:
            st = c[rem]
            lines.append(f"{rem}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])}")
        lines.append("by_hour:")
        for h, st in c["by_hour"].items():
            lines.append(f"  {h}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f} mdd={pct(st['mdd'])}")
        lines.append("by_exit_reason:")
        for reason, st in c["by_reason"].items():
            lines.append(f"  {reason}: n={st['n']} sum={pct(st['sum'])} avg={pct(st['avg'])} sh={st['sharpe_like']:+.2f}")
        lines.append("```\n")

    out = PROJECT_ROOT / "output" / f"a-night-rel5-v1-shadow-fresh-today-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
