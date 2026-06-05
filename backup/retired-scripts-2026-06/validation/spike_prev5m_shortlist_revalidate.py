#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
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

from spike_prev5m_alpha_search import (  # noqa: E402
    BJ,
    COST,
    Spec,
    apply_spec,
    pct,
    stats_for,
    window_defs,
    new_market_top20,
)
from radar_alpha_skills_lab.signal_control import (  # noqa: E402
    _stats_for_rows,
    attach_managed_1h,
    load_snapshot_rows,
    stat,
)
from radar_alpha_skills_lab.score_regime import enrich_rows, matched_random_distribution, top_fraction_per_timestamp  # noqa: E402


def core_filter(rows: Sequence[Mapping[str, Any]], session: str) -> list[dict[str, Any]]:
    out = []
    for r0 in rows:
        r = dict(r0)
        h = int(r.get("hour_bjt") or 0)
        if session == "core_20_08":
            ok = h >= 20 or h < 8
        elif session == "night_20_04":
            ok = h >= 20 or h < 4
        elif session == "dawn_04_08":
            ok = 4 <= h < 8
        else:
            ok = True
        if ok:
            out.append(r)
    return out


def baseline_select(rows: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    if name == "BASE_A_core_all":
        return core_filter(new_market_top20(rows), "core_20_08")
    if name == "BASE_A_night_all":
        return core_filter(new_market_top20(rows), "night_20_04")
    if name == "BASE_A_dawn_all":
        return core_filter(new_market_top20(rows), "dawn_04_08")
    raise KeyError(name)


def contrib(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bysym: defaultdict[str, float] = defaultdict(float)
    cnt: Counter[str] = Counter()
    bydate: defaultdict[str, list[float]] = defaultdict(list)
    byhour: defaultdict[int, list[float]] = defaultdict(list)
    for r in rows:
        v = float(r["managed_1h"]) - COST
        s = str(r.get("symbol"))
        bysym[s] += v
        cnt[s] += 1
        bydate[str(r.get("date_bjt"))].append(v)
        byhour[int(r.get("hour_bjt") or 0)].append(v)
    ranked = sorted(bysym.items(), key=lambda kv: kv[1], reverse=True)
    rem_top5 = {s for s, _ in ranked[:5]}
    return {
        "top": [(s, v, cnt[s]) for s, v in ranked[:10]],
        "bottom": [(s, v, cnt[s]) for s, v in sorted(bysym.items(), key=lambda kv: kv[1])[:8]],
        "remove_top5": stat(float(r["managed_1h"]) - COST for r in rows if str(r.get("symbol")) not in rem_top5),
        "by_date": {k: stat(v) for k, v in sorted(bydate.items())},
        "by_hour": {str(k): stat(v) for k, v in sorted(byhour.items())},
    }


def flags(st: Mapping[str, Any], rand: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "gross_avg_gt_rand_p95": st["gross"]["avg"] > rand["avg"]["p95"],
        "gross_sum_gt_rand_p95": st["gross"]["sum"] > rand["sum"]["p95"],
        "taker_avg_pos": st["taker"]["avg"] > 0,
        "cap5_pos": st["cap5"]["comp"] > 0,
        "cap10_pos": st["cap10"]["comp"] > 0,
        "remove_top5_avg_pos": False,
    }


def main() -> int:
    sims = 300
    out_prefix = PROJECT_ROOT / "output" / f"prev5m-a-shortlist-revalidate-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    latest_probe, latest_meta = load_snapshot_rows(hours=1)
    latest_complete = datetime.fromisoformat(latest_meta["complete_end_utc"])
    windows = window_defs(latest_complete)
    rows_by_window: dict[str, list[dict[str, Any]]] = {}
    window_meta: dict[str, Any] = {}
    for wname, end in windows:
        rows0, meta = load_snapshot_rows(hours=24, end_utc=end, now_utc=datetime.now(timezone.utc) + timedelta(days=1))
        rows_path, path_meta = attach_managed_1h(rows0)
        rows = enrich_rows([r for r in rows_path if r.get("managed_1h") is not None])
        rows_by_window[wname] = rows
        window_meta[wname] = {"load": meta, "path": path_meta}

    specs: list[tuple[str, Spec | None]] = [
        ("BASE_A_core_all", None),
        ("BASE_A_night_all", None),
        ("BASE_A_dawn_all", None),
        ("R1_core_top10_prev5ret_risk70", Spec("R1_core_top10_prev5ret_risk70", "core_20_08", "prev5m_ret", 0.10, fomo_max=70, warning_max=70)),
        ("R2_core_top20_rel5_rel0", Spec("R2_core_top20_rel5_rel0", "core_20_08", "symbol_rel5_vs_btc", 0.20, rel5_min=0.0)),
        ("R3_core_top33_prev5ret_rel0002", Spec("R3_core_top33_prev5ret_rel0002", "core_20_08", "prev5m_ret", 0.33, rel5_min=0.002)),
        ("R4_night_top10_rel5_wick50", Spec("R4_night_top10_rel5_wick50", "night_20_04", "symbol_rel5_vs_btc", 0.10, wick_max=0.50)),
        ("R5_night_top20_rel5_rel0_wick50", Spec("R5_night_top20_rel5_rel0_wick50", "night_20_04", "symbol_rel5_vs_btc", 0.20, rel5_min=0.0, wick_max=0.50)),
        ("R6_night_top33_rel5_rel0002", Spec("R6_night_top33_rel5_rel0002", "night_20_04", "symbol_rel5_vs_btc", 0.33, rel5_min=0.002)),
        ("R7_strict_night_top10_prev5ret_rel0002_wick50", Spec("R7_strict_night_top10_prev5ret_rel0002_wick50", "night_20_04", "prev5m_ret", 0.10, rel5_min=0.002, wick_max=0.50, fomo_max=70, warning_max=70)),
        ("R8_strict_core_top10_rel5_rel0002_wick50", Spec("R8_strict_core_top10_rel5_rel0002_wick50", "core_20_08", "symbol_rel5_vs_btc", 0.10, rel5_min=0.002, wick_max=0.50, fomo_max=70, warning_max=70)),
    ]

    result: dict[str, Any] = {"generated_utc": datetime.now(timezone.utc).isoformat(), "sims": sims, "window_meta": window_meta, "sets": {}}
    lines: list[str] = [
        "# Prev5m A shortlist revalidation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "Method: A=new_market_top20; core/night/dawn windows; managed_1h next full 1m open; all-taker 8bp; same-timestamp random baseline; 300 sims.",
        "",
        "## Summary",
        "```text",
    ]
    for si, (name, spec) in enumerate(specs):
        result["sets"][name] = {"spec": None if spec is None else spec.__dict__, "windows": {}}
        lines.append(name)
        for wi, (wname, rows) in enumerate(rows_by_window.items()):
            if spec is None:
                selected = baseline_select(rows, name)
            else:
                selected = apply_spec(rows, spec)
            selected = sorted(selected, key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
            if len(selected) < 20:
                block = {"n": len(selected), "insufficient": True}
                result["sets"][name]["windows"][wname] = block
                lines.append(f"  {wname:<16} insufficient n={len(selected)}")
                continue
            st = stats_for(selected)
            rand = matched_random_distribution(rows, selected, sims=sims, seed=2026060300 + si * 17 + wi)
            c = contrib(selected)
            fl = flags(st, rand)
            fl["remove_top5_avg_pos"] = c["remove_top5"]["avg"] > 0
            block = {"n": len(selected), "stats": st, "random": rand, "contrib": c, "flags": fl}
            result["sets"][name]["windows"][wname] = block
            t = st["taker"]
            lines.append(
                f"  {wname:<16} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} med={pct(t['median']):>8} "
                f"win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} mdd={pct(t['mdd']):>8} "
                f"cap5={pct(st['cap5']['comp']):>8} cap10={pct(st['cap10']['comp']):>8} "
                f"rand_p95_avg={pct(rand['avg']['p95']):>8} rand_p95_sum={pct(rand['sum']['p95']):>8} "
                f"remTop5_avg={pct(c['remove_top5']['avg']):>8} flags={fl}"
            )
        lines.append("")
    lines.append("```")
    lines.append("")
    lines.append("## Top/bottom symbol details")
    for name, _spec in specs:
        lines.append("")
        lines.append(f"### {name}")
        for wname, block in result["sets"][name]["windows"].items():
            if block.get("insufficient"):
                continue
            top = ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in block["contrib"]["top"][:8])
            bottom = ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in block["contrib"]["bottom"][:6])
            lines.append(f"- {wname} top: {top}")
            lines.append(f"- {wname} bottom: {bottom}")

    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    md_path.write_text("\n".join(lines) + "\n")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
