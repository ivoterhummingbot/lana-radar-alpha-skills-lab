#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.signal_control import (  # noqa: E402
    COSTS,
    _stats_for_rows,
    attach_managed_1h,
    cap_portfolio,
    load_snapshot_rows,
    stat,
)
from radar_alpha_skills_lab.score_regime import enrich_rows, matched_random_distribution, top_fraction_per_timestamp  # noqa: E402

BJ = timezone(timedelta(hours=8))
COST = COSTS["all_taker_8bp_total"]


def pct(x: float | None) -> str:
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return "na"
    return f"{float(x) * 100:+.2f}%"


def fnum(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = float(row.get(key) if row.get(key) is not None else default)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def bjt_hour(row: Mapping[str, Any]) -> int:
    return int(row.get("hour_bjt") or 0)


def session_filter(rows: Sequence[Mapping[str, Any]], session: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r0 in rows:
        r = dict(r0)
        h = bjt_hour(r)
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


def new_market_top20(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return top_fraction_per_timestamp(rows, "market_confirmation_score", 0.20)


@dataclass(frozen=True)
class Spec:
    name: str
    session: str = "core_20_08"
    rank_field: str = "prev5m_confirmation_score"
    top_fraction: float = 0.20
    rel5_min: float | None = None
    prev5_min: float | None = None
    prev5_max: float | None = None
    wick_max: float | None = None
    fomo_max: float | None = None
    warning_max: float | None = None
    btc_allow_only: bool = False
    min_prev5_confirm: float | None = None


def apply_spec(rows: Sequence[Mapping[str, Any]], spec: Spec) -> list[dict[str, Any]]:
    base = new_market_top20(rows)
    base = session_filter(base, spec.session)
    filtered: list[dict[str, Any]] = []
    for r0 in base:
        r = dict(r0)
        if spec.rel5_min is not None and fnum(r, "symbol_rel5_vs_btc") < spec.rel5_min:
            continue
        if spec.prev5_min is not None and fnum(r, "prev5m_ret") < spec.prev5_min:
            continue
        if spec.prev5_max is not None and fnum(r, "prev5m_ret") > spec.prev5_max:
            continue
        if spec.wick_max is not None and fnum(r, "prev5m_upper_wick_ratio") > spec.wick_max:
            continue
        if spec.fomo_max is not None and fnum(r, "fomo_risk_score") > spec.fomo_max:
            continue
        if spec.warning_max is not None and fnum(r, "warning_score") > spec.warning_max:
            continue
        if spec.btc_allow_only and str(r.get("btc_gate_permission")) not in {"allow", "None", "none"}:
            continue
        if spec.min_prev5_confirm is not None and fnum(r, "prev5m_confirmation_score") < spec.min_prev5_confirm:
            continue
        filtered.append(r)
    if not filtered:
        return []
    return top_fraction_per_timestamp(filtered, spec.rank_field, spec.top_fraction)


def stats_for(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "gross": _stats_for_rows(rows, 0.0),
        "taker": _stats_for_rows(rows, COST),
        "cap5": cap_portfolio(rows, "managed_1h", COST, 5),
        "cap10": cap_portfolio(rows, "managed_1h", COST, 10),
        "cap20": cap_portfolio(rows, "managed_1h", COST, 20),
    }


def pass_flags(st: Mapping[str, Any], rand: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "gross_avg_gt_rand_p95": st["gross"]["avg"] > rand["avg"]["p95"],
        "gross_sum_gt_rand_p95": st["gross"]["sum"] > rand["sum"]["p95"],
        "taker_avg_pos": st["taker"]["avg"] > 0,
        "cap5_pos": st["cap5"]["comp"] > 0,
        "cap10_pos": st["cap10"]["comp"] > 0,
    }


def spec_grid() -> list[Spec]:
    specs: list[Spec] = []
    sessions = ["core_20_08", "night_20_04", "dawn_04_08"]
    # Keep this as a semantic spike, not an overfit optimizer: coarse, explainable knobs only.
    rank_fields = [
        "prev5m_confirmation_score",
        "symbol_rel5_vs_btc",
        "prev5m_ret",
        "momentum_confirmation_score",
    ]
    fracs = [0.10, 0.20, 0.33]
    rels = [None, 0.0, 0.002]
    prev_bands = [
        (None, None, "allprev"),
        (-0.001, 0.020, "not_dump_not_fomo"),
        (0.0, 0.020, "pos_not_fomo"),
    ]
    wick_opts = [None, 0.50]
    # Coarse risk thresholds; actual score scale is 0-100-ish in the sidecar.
    risk_opts = [(None, None, "riskAny"), (70.0, 70.0, "risk70")]
    btc_opts = [False]
    for session in sessions:
        for rank in rank_fields:
            for frac in fracs:
                for rel in rels:
                    for pmin, pmax, ptag in prev_bands:
                        for wick in wick_opts:
                            for fomo, warn, rtag in risk_opts:
                                for btc in btc_opts:
                                    parts = [
                                        "A",
                                        session,
                                        f"top{int(frac*100)}_{rank}",
                                        f"rel{rel if rel is not None else 'Any'}",
                                        ptag,
                                        f"wick{wick if wick is not None else 'Any'}",
                                        rtag,
                                        "btcAllow" if btc else "btcAny",
                                    ]
                                    specs.append(
                                        Spec(
                                            name="__".join(str(p).replace(".", "p").replace("-", "m") for p in parts),
                                            session=session,
                                            rank_field=rank,
                                            top_fraction=frac,
                                            rel5_min=rel,
                                            prev5_min=pmin,
                                            prev5_max=pmax,
                                            wick_max=wick,
                                            fomo_max=fomo,
                                            warning_max=warn,
                                            btc_allow_only=btc,
                                        )
                                    )
    # Semantic baselines to make reports readable.
    specs.extend([
        Spec("BASE_A_core20_08_top20_prev5", "core_20_08", "prev5m_confirmation_score", 0.20),
        Spec("BASE_A_night20_04_top20_prev5", "night_20_04", "prev5m_confirmation_score", 0.20),
        Spec("BASE_A_dawn04_08_top20_prev5", "dawn_04_08", "prev5m_confirmation_score", 0.20),
        Spec("SEM_A_core_rel0_posprev_notfomo_top20_prev5", "core_20_08", "prev5m_confirmation_score", 0.20, rel5_min=0.0, prev5_min=0.0, prev5_max=0.020, wick_max=0.50),
        Spec("SEM_A_core_rel0_posprev_notfomo_risk70_top20_prev5", "core_20_08", "prev5m_confirmation_score", 0.20, rel5_min=0.0, prev5_min=0.0, prev5_max=0.020, wick_max=0.50, fomo_max=70, warning_max=70),
    ])
    # Deduplicate by fields, preserving name of first occurrence.
    seen: set[tuple[Any, ...]] = set()
    out: list[Spec] = []
    for s in specs:
        key = tuple(asdict(s).items())[1:]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def window_defs(latest_complete: datetime) -> list[tuple[str, datetime]]:
    return [
        ("BJT_2026-05-31", datetime(2026, 5, 31, 16, 0, tzinfo=timezone.utc)),
        ("BJT_2026-06-01", datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)),
        ("BJT_2026-06-02", datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)),
        ("latest24", latest_complete),
    ]


def summarize_symbol_contrib(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bysym: defaultdict[str, float] = defaultdict(float)
    cnt: Counter[str] = Counter()
    for r in rows:
        s = str(r.get("symbol"))
        bysym[s] += float(r["managed_1h"]) - COST
        cnt[s] += 1
    top = sorted(bysym.items(), key=lambda kv: kv[1], reverse=True)[:10]
    bottom = sorted(bysym.items(), key=lambda kv: kv[1])[:8]
    if top:
        remove_top5_symbols = {s for s, _ in top[:5]}
        rem = [r for r in rows if str(r.get("symbol")) not in remove_top5_symbols]
        rem_stat = _stats_for_rows(rem, COST)
    else:
        rem_stat = stat([])
    return {
        "top": [(s, v, cnt[s]) for s, v in top],
        "bottom": [(s, v, cnt[s]) for s, v in bottom],
        "remove_top5_taker": rem_stat,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=80)
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--out-prefix", type=Path, default=PROJECT_ROOT / "output" / f"prev5m-a-core-alpha-search-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    args = ap.parse_args()

    latest_probe_rows, latest_meta = load_snapshot_rows(hours=1)
    latest_complete = datetime.fromisoformat(latest_meta["complete_end_utc"])
    specs = spec_grid()
    windows = window_defs(latest_complete)

    result: dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": "A=new_market_top20 per timestamp; core windows; prev5m/risk/rank grid; managed_1h next full 1m open; all-taker 8bp; rolling <=24h windows",
        "spec_count": len(specs),
        "windows": {},
        "ranked": [],
    }

    rows_by_window: dict[str, list[dict[str, Any]]] = {}
    for wname, end in windows:
        rows0, meta = load_snapshot_rows(hours=24, end_utc=end, now_utc=datetime.now(timezone.utc) + timedelta(days=1))
        rows_path, path_meta = attach_managed_1h(rows0)
        rows = enrich_rows([r for r in rows_path if r.get("managed_1h") is not None])
        rows_by_window[wname] = rows
        result["windows"][wname] = {"end_utc": end.isoformat(), "load_meta": meta, "path_meta": path_meta, "evaluations": {}}

    for idx, spec in enumerate(specs):
        spec_summary: dict[str, Any] = {"spec": asdict(spec), "windows": {}}
        window_scores: list[float] = []
        pass_windows = 0
        pos_cap5_windows = 0
        pos_cap10_windows = 0
        enough_windows = 0
        latest_block: dict[str, Any] | None = None
        for wname, rows in rows_by_window.items():
            selected = sorted(apply_spec(rows, spec), key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
            if len(selected) < 20:
                spec_summary["windows"][wname] = {"n": len(selected), "insufficient": True}
                continue
            enough_windows += 1
            st = stats_for(selected)
            rand = matched_random_distribution(rows, selected, sims=args.sims, seed=20260603 + idx)
            flags = pass_flags(st, rand)
            contrib = summarize_symbol_contrib(selected)
            block = {"n": len(selected), "stats": st, "random": rand, "flags": flags, "contrib": contrib}
            spec_summary["windows"][wname] = block
            pass_windows += int(flags["gross_avg_gt_rand_p95"] and flags["gross_sum_gt_rand_p95"] and flags["taker_avg_pos"])
            pos_cap5_windows += int(flags["cap5_pos"])
            pos_cap10_windows += int(flags["cap10_pos"])
            # Conservative objective: cap5/cap10 and taker avg, penalize MDD and star dependence.
            t = st["taker"]
            score = (
                st["cap5"]["comp"] * 8
                + st["cap10"]["comp"] * 4
                + t["avg"] * 20
                + t["sharpe_like"] * 0.03
                + min(0.0, contrib["remove_top5_taker"]["avg"]) * 10
                + t["mdd"] * 0.5
            )
            window_scores.append(score)
            if wname == "latest24":
                latest_block = block
        if enough_windows == 0:
            continue
        worst_score = min(window_scores) if window_scores else -999.0
        avg_score = sum(window_scores) / len(window_scores) if window_scores else -999.0
        latest_cap5 = latest_block["stats"]["cap5"]["comp"] if latest_block else -999.0
        latest_avg = latest_block["stats"]["taker"]["avg"] if latest_block else -999.0
        latest_pass = bool(latest_block and latest_block["flags"]["taker_avg_pos"] and latest_block["flags"]["cap5_pos"])
        rank_key = (
            pass_windows,
            pos_cap5_windows,
            pos_cap10_windows,
            latest_pass,
            latest_cap5,
            worst_score,
            avg_score,
            latest_avg,
        )
        result["ranked"].append({"name": spec.name, "rank_key": rank_key, "summary": spec_summary})

    result["ranked"].sort(key=lambda x: x["rank_key"], reverse=True)
    result["ranked"] = result["ranked"][: args.top_n]

    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out_prefix.with_suffix(".json")
    md_path = args.out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")

    lines: list[str] = []
    lines.append("# Prev5m + A core alpha search")
    lines.append("")
    lines.append(f"generated_utc: `{result['generated_utc']}`")
    lines.append("")
    lines.append("Method: A=`new_market_top20`; core windows `04-08 + 20-04 BJT`; execution `next full 1m open + managed_1h`; cost all-taker 8bp; random baseline same-timestamp.")
    lines.append("")
    lines.append(f"spec_count={len(specs)} top_n={args.top_n} sims={args.sims}")
    lines.append("")
    lines.append("## Top candidates")
    lines.append("```text")
    for i, item in enumerate(result["ranked"][: args.top_n], 1):
        name = item["name"]
        rk = item["rank_key"]
        lines.append(f"#{i:02d} {name} rank={rk}")
        for wname, block in item["summary"]["windows"].items():
            if block.get("insufficient"):
                lines.append(f"  {wname:<16} insufficient n={block['n']}")
                continue
            t = block["stats"]["taker"]
            c5 = block["stats"]["cap5"]
            c10 = block["stats"]["cap10"]
            rand = block["random"]
            flags = block["flags"]
            rem = block["contrib"]["remove_top5_taker"]
            lines.append(
                f"  {wname:<16} n={t['n']:4d} sum={pct(t['sum']):>8} avg={pct(t['avg']):>8} med={pct(t['median']):>8} "
                f"win={t['win']*100:5.1f}% sh={t['sharpe_like']:+5.2f} mdd={pct(t['mdd']):>8} "
                f"cap5={pct(c5['comp']):>8} cap10={pct(c10['comp']):>8} rand_p95_avg={pct(rand['avg']['p95']):>8} "
                f"remTop5_avg={pct(rem['avg']):>8} flags={flags}"
            )
        lines.append("")
    lines.append("```")
    lines.append("")
    lines.append("## Best candidate details")
    for item in result["ranked"][: min(8, args.top_n)]:
        lines.append("")
        lines.append(f"### {item['name']}")
        spec = item["summary"]["spec"]
        lines.append("```json")
        lines.append(json.dumps(spec, ensure_ascii=False, indent=2))
        lines.append("```")
        for wname, block in item["summary"]["windows"].items():
            if block.get("insufficient"):
                continue
            top = ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in block["contrib"]["top"][:8])
            bottom = ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in block["contrib"]["bottom"][:6])
            lines.append(f"- {wname} top: {top}")
            lines.append(f"- {wname} bottom: {bottom}")
    md_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print("top", result["ranked"][0]["name"] if result["ranked"] else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
