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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from spike_prev5m_alpha_search import COST, Spec, apply_spec, new_market_top20, pct, stats_for  # noqa: E402
from radar_alpha_skills_lab.signal_control import attach_managed_1h, cap_portfolio, load_snapshot_rows, stat  # noqa: E402
from radar_alpha_skills_lab.score_regime import enrich_rows  # noqa: E402

BJ = timezone(timedelta(hours=8))
FREEZE_UTC = datetime(2026, 6, 3, 4, 15, tzinfo=timezone.utc)  # BJT 12:15 after overfit validation
TODAY_BJT_START_UTC = datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)
SIMS = 500

CANDIDATES: list[Spec] = [
    Spec("R6_A_night_rel5_v1_shadow", "night_20_04", "symbol_rel5_vs_btc", 0.33, rel5_min=0.002),
    Spec(
        "R7_A_night_prev5_strict_v1",
        "night_20_04",
        "prev5m_ret",
        0.10,
        rel5_min=0.002,
        wick_max=0.50,
        fomo_max=70,
        warning_max=70,
    ),
    Spec(
        "R8_A_core_rel5_strict_v1",
        "core_20_08",
        "symbol_rel5_vs_btc",
        0.10,
        rel5_min=0.002,
        wick_max=0.50,
        fomo_max=70,
        warning_max=70,
    ),
]


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


def session_universe(rows: Sequence[Mapping[str, Any]], session: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in new_market_top20(rows):
        h = int(r.get("hour_bjt") or 0)
        if session == "night_20_04":
            ok = h >= 20 or h < 4
        elif session == "core_20_08":
            ok = h >= 20 or h < 8
        elif session == "dawn_04_08":
            ok = 4 <= h < 8
        else:
            ok = True
        if ok:
            out.append(dict(r))
    return out


def contribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_sym: defaultdict[str, list[float]] = defaultdict(list)
    by_hour: defaultdict[int, list[float]] = defaultdict(list)
    by_reason: defaultdict[str, list[float]] = defaultdict(list)
    rows_sorted = sorted(rows, key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
    for r in rows_sorted:
        v = float(r["managed_1h"]) - COST
        by_sym[str(r.get("symbol"))].append(v)
        by_hour[int(r.get("hour_bjt") or 0)].append(v)
        by_reason[str(r.get("managed_1h_reason"))].append(v)
    ranked = sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1], reverse=True)
    out: dict[str, Any] = {
        "sample_trades": [
            {
                "ts_bjt": r.get("ts_bjt"),
                "symbol": r.get("symbol"),
                "rel5": r.get("symbol_rel5_vs_btc"),
                "prev5m_ret": r.get("prev5m_ret"),
                "pnl_taker": float(r["managed_1h"]) - COST,
                "reason": r.get("managed_1h_reason"),
            }
            for r in rows_sorted[:30]
        ],
        "top_symbols": ranked[:12],
        "bottom_symbols": sorted(((s, sum(v), len(v)) for s, v in by_sym.items()), key=lambda x: x[1])[:10],
        "by_hour": {str(k): stat(v) for k, v in sorted(by_hour.items())},
        "by_reason": {k: stat(v) for k, v in sorted(by_reason.items())},
    }
    for n in [1, 3, 5, 10]:
        rem = {s for s, _sum, _n in ranked[:n]}
        out[f"remove_top{n}"] = stat(float(r["managed_1h"]) - COST for r in rows if str(r.get("symbol")) not in rem)
    return out


def summarize(label: str, rows: list[dict[str, Any]], universe: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    if not rows:
        return {"label": label, "n": 0, "stats": {}, "random_same_ts": {}, "cap5": {}, "cap10": {}, "contribution": {}, "flags": {}}
    st = stats_for(rows)
    rand = random_same_ts(universe, rows, sims=SIMS, seed=seed)
    cap5 = cap_portfolio(rows, "managed_1h", COST, 5)
    cap10 = cap_portfolio(rows, "managed_1h", COST, 10)
    contrib = contribution(rows)
    flags = {
        "avg_gt_rand_p95": st["taker"]["avg"] > rand["avg"]["p95"],
        "sum_gt_rand_p95": st["taker"]["sum"] > rand["sum"]["p95"],
        "sh_gt_rand_p95": st["taker"]["sharpe_like"] > rand["sharpe_like"]["p95"],
        "cap5_pos": cap5["comp"] > 0,
        "cap10_pos": cap10["comp"] > 0,
        "remove_top5_avg_pos": contrib["remove_top5"]["avg"] > 0,
    }
    return {"label": label, "n": len(rows), "stats": st, "random_same_ts": rand, "cap5": cap5, "cap10": cap10, "contribution": contrib, "flags": flags}


def fmt(block: Mapping[str, Any]) -> str:
    label = str(block["label"])
    if block["n"] == 0:
        return f"{label:<34} n=0"
    t = block["stats"]["taker"]
    rand = block["random_same_ts"]
    return (
        f"{label:<34} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} med={pct(t['median']):>8} "
        f"win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} mdd={pct(t['mdd']):>8} "
        f"cap5={pct(block['cap5']['comp']):>8} cap10={pct(block['cap10']['comp']):>8} "
        f"rand95_avg={pct(rand['avg']['p95']):>8} rand95_sum={pct(rand['sum']['p95']):>8} "
        f"remT5avg={pct(block['contribution']['remove_top5']['avg']):>8} flags={block['flags']}"
    )


def main() -> int:
    _probe, latest_meta = load_snapshot_rows(hours=1)
    complete_end = datetime.fromisoformat(latest_meta["complete_end_utc"])
    hours = (complete_end - TODAY_BJT_START_UTC).total_seconds() / 3600 + 0.2
    rows0, load_meta = load_snapshot_rows(hours=hours, end_utc=complete_end)
    rows_path, path_meta = attach_managed_1h(rows0)
    rows = enrich_rows([r for r in rows_path if r.get("managed_1h") is not None])
    rows_fresh = [r for r in rows if r["ts_dt"] >= FREEZE_UTC]

    result: dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "freeze_utc": FREEZE_UTC.isoformat(),
        "freeze_bjt": FREEZE_UTC.astimezone(BJ).strftime("%Y-%m-%d %H:%M"),
        "complete_end_utc": complete_end.isoformat(),
        "complete_end_bjt": complete_end.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "load_meta": load_meta,
        "path_meta": path_meta,
        "sets": {},
    }

    lines = [
        "# Other prev5m candidates fresh validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        f"freeze_bjt: `{result['freeze_bjt']}`",
        f"complete_end_bjt: `{result['complete_end_bjt']}`",
        "",
        "Fixed candidates only; no re-search.",
        "",
        "## Fresh after freeze summary",
        "```text",
    ]

    for i, spec in enumerate(CANDIDATES):
        selected_today = apply_spec(rows, spec)
        selected_fresh = [r for r in selected_today if r["ts_dt"] >= FREEZE_UTC]
        universe_today = session_universe(rows, spec.session)
        universe_fresh = [r for r in universe_today if r["ts_dt"] >= FREEZE_UTC]
        b_fresh = summarize(spec.name, selected_fresh, universe_fresh, 2026060300 + i * 101)
        b_today = summarize(spec.name + "__today", selected_today, universe_today, 2026060400 + i * 101)
        result["sets"][spec.name] = {"spec": spec.__dict__, "today": b_today, "fresh": b_fresh, "universe_fresh_n": len(universe_fresh)}
        lines.append(fmt(b_fresh))
    lines.extend(["```", "", "## Today all available same rule summary (context, not strict fresh)", "```text"])
    for spec in CANDIDATES:
        lines.append(fmt(result["sets"][spec.name]["today"]))
    lines.extend(["```", ""])

    for spec in CANDIDATES:
        block = result["sets"][spec.name]["fresh"]
        lines.append(f"## {spec.name} fresh details")
        if block["n"] == 0:
            lines.append("No fresh eligible trades.")
            lines.append("")
            continue
        c = block["contribution"]
        lines.append("```text")
        lines.append("sample_trades:")
        for r in c["sample_trades"]:
            lines.append(
                f"  {r['ts_bjt']} {r['symbol']:<10} rel5={pct(r['rel5'])} prev5={pct(r['prev5m_ret'])} pnl={pct(r['pnl_taker'])} {r['reason']}"
            )
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

    out = PROJECT_ROOT / "output" / f"prev5m-other-fresh-candidates-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
