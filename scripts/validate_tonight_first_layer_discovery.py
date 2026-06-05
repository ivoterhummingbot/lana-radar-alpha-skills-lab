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

from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.old_radar_alpha import load_old_radar_rows, parse_ts  # noqa: E402
from radar_alpha_skills_lab.radar_effectiveness import (  # noqa: E402
    _decorate_old_rows,
    attach_short_horizon_ohlc,
    candidate_top_fraction_by_ts,
    random_same_timestamp_distribution,
)
from radar_alpha_skills_lab.score_regime import bjt_session  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, load_snapshot_rows, pct, stat  # noqa: E402

BJ = timezone(timedelta(hours=8))
SIMS = 500
HORIZONS = ["15m", "30m", "1h"]
TONIGHT_START_BJT = datetime(2026, 6, 3, 20, 0, tzinfo=BJ)
TONIGHT_START_UTC = TONIGHT_START_BJT.astimezone(timezone.utc)


def q(values: list[float], p: float) -> float:
    xs = sorted(values)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))]


def values(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def top_symbol(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[str(r.get("symbol"))].append(float(r[key]))
    ranked = sorted(((s, sum(v), len(v)) for s, v in by.items()), key=lambda x: x[1], reverse=True)
    out: dict[str, Any] = {"top": ranked[:10], "bottom": sorted(((s, sum(v), len(v)) for s, v in by.items()), key=lambda x: x[1])[:8]}
    for n in [1, 3, 5, 10]:
        removed = {s for s, _v, _n in ranked[:n]}
        out[f"remove_top{n}"] = stat(float(r[key]) for r in rows if r.get(key) is not None and str(r.get("symbol")) not in removed)
    return out


def by_hour(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    by: defaultdict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[int(r.get("hour_bjt") or 0)].append(float(r[key]))
    return {str(h): stat(v) for h, v in sorted(by.items())}


def summarize(label: str, universe: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], *, seed: int) -> dict[str, Any]:
    block: dict[str, Any] = {"label": label, "rows": len(rows), "symbols": len({str(r.get("symbol")) for r in rows}), "horizons": {}}
    for i, h in enumerate(HORIZONS):
        ret_key = f"ret_{h}"
        mfe_key = f"mfe_{h}"
        ret_vals = values(rows, ret_key)
        mfe_vals = values(rows, mfe_key)
        ret_stat = stat(ret_vals)
        mfe_stat = stat(mfe_vals)
        rand_ret = random_same_timestamp_distribution(universe, rows, ret_key, sims=SIMS, seed=seed + i * 101) if rows and len(rows) < len(universe) else {}
        rand_mfe = random_same_timestamp_distribution(universe, rows, mfe_key, sims=SIMS, seed=seed + 17 + i * 101) if rows and len(rows) < len(universe) else {}
        ret_p95 = rand_ret.get("avg", {}).get("p95")
        mfe_p95 = rand_mfe.get("avg", {}).get("p95")
        block["horizons"][h] = {
            "close_return": ret_stat,
            "mfe": mfe_stat,
            "mfe_hit_1pct": (sum(1 for v in mfe_vals if v >= 0.01) / len(mfe_vals)) if mfe_vals else 0.0,
            "mfe_hit_2pct": (sum(1 for v in mfe_vals if v >= 0.02) / len(mfe_vals)) if mfe_vals else 0.0,
            "random_same_ts_close": rand_ret,
            "random_same_ts_mfe": rand_mfe,
            "close_pass_p95": (ret_stat["avg"] > ret_p95) if ret_p95 is not None else None,
            "mfe_pass_p95": (mfe_stat["avg"] > mfe_p95) if mfe_p95 is not None else None,
            "top_symbol_close": top_symbol(rows, ret_key),
            "top_symbol_mfe": top_symbol(rows, mfe_key),
            "by_hour_close": by_hour(rows, ret_key),
        }
    return block


def line(name: str, block: Mapping[str, Any]) -> str:
    parts = [f"{name:<32} n={block['rows']:4d} sym={block['symbols']:3d}"]
    flags = []
    for h in HORIZONS:
        hb = block["horizons"][h]
        st = hb["close_return"]
        mfe = hb["mfe"]
        ret_p95 = hb.get("random_same_ts_close", {}).get("avg", {}).get("p95")
        mfe_p95 = hb.get("random_same_ts_mfe", {}).get("avg", {}).get("p95")
        cp = "C" if hb["close_pass_p95"] else "-"
        mp = "M" if hb["mfe_pass_p95"] else "-"
        flags.append(cp + mp)
        if h == "1h":
            parts.append(
                f"1h_ret={pct(st['avg'])}/{pct(ret_p95) if ret_p95 is not None else 'n/a'} "
                f"1h_mfe={pct(mfe['avg'])}/{pct(mfe_p95) if mfe_p95 is not None else 'n/a'} "
                f"hit1={hb['mfe_hit_1pct']*100:.1f}% hit2={hb['mfe_hit_2pct']*100:.1f}%"
            )
    parts.insert(1, "flags=" + "/".join(flags))
    return " | ".join(parts)


def details_lines(name: str, block: Mapping[str, Any]) -> list[str]:
    out = [f"## {name}", "", "```text", f"rows={block['rows']} symbols={block['symbols']}"]
    for h in HORIZONS:
        hb = block["horizons"][h]
        st = hb["close_return"]
        mfe = hb["mfe"]
        ret_p95 = hb.get("random_same_ts_close", {}).get("avg", {}).get("p95")
        mfe_p95 = hb.get("random_same_ts_mfe", {}).get("avg", {}).get("p95")
        out.append(
            f"{h}: close avg={pct(st['avg'])} med={pct(st['median'])} win={st['win']*100:.1f}% sh={st['sharpe_like']:+.2f} sum={pct(st['sum'])} "
            f"| mfe avg={pct(mfe['avg'])} med={pct(mfe['median'])} hit1={hb['mfe_hit_1pct']*100:.1f}% hit2={hb['mfe_hit_2pct']*100:.1f}% "
            f"| rand_close_p95={pct(ret_p95) if ret_p95 is not None else 'n/a'} pass={hb['close_pass_p95']} "
            f"rand_mfe_p95={pct(mfe_p95) if mfe_p95 is not None else 'n/a'} mfe_pass={hb['mfe_pass_p95']}"
        )
    h1 = block["horizons"]["1h"]
    out.append("top_1h_close=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in h1["top_symbol_close"]["top"][:10]))
    out.append("bottom_1h_close=" + ", ".join(f"{s} {pct(v)}/{n}" for s, v, n in h1["top_symbol_close"]["bottom"][:8]))
    for rem in ["remove_top1", "remove_top3", "remove_top5"]:
        st = h1["top_symbol_close"][rem]
        out.append(f"1h_close_{rem}: n={st['n']} avg={pct(st['avg'])} sum={pct(st['sum'])} sh={st['sharpe_like']:+.2f}")
    out.append("by_hour_1h_close:")
    for h, st in h1["by_hour_close"].items():
        out.append(f"  {h}: n={st['n']} avg={pct(st['avg'])} sum={pct(st['sum'])} sh={st['sharpe_like']:+.2f}")
    out.extend(["```", ""])
    return out


def main() -> int:
    # New radar: load recent snapshots and recompute first-layer 15m OHLC outcomes.
    now_utc = datetime.now(timezone.utc)
    hours = max(5.0, (now_utc - TONIGHT_START_UTC).total_seconds() / 3600 + 3.0)
    new_rows0, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=hours)
    new_path, new_path_meta = attach_short_horizon_ohlc(new_rows0)
    new_tonight = [r for r in new_path if r["ts_dt"] >= TONIGHT_START_UTC]
    new_universe = sorted(new_tonight, key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
    A = candidate_top_fraction_by_ts(new_universe, "market_confirmation_score", 0.20)

    # Old radar: load old rows and recompute same first-layer semantics from OHLC.
    old_rows0, old_meta = load_old_radar_rows(source=DEFAULT_SOURCE)
    old_cutoff = parse_ts(str(old_meta["complete_cutoffs"]["1h"]))
    old_ready = _decorate_old_rows(old_rows0, old_cutoff)
    old_recent = [r for r in old_ready if r["ts_dt"] >= TONIGHT_START_UTC]
    old_path, old_path_meta = attach_short_horizon_ohlc(old_recent)
    old_universe = sorted(old_path, key=lambda r: (r["ts_dt"], str(r.get("symbol"))))
    B = [dict(r) for r in old_universe if str(r.get("decision_status")) == "watch_hot"]
    C = candidate_top_fraction_by_ts([dict(r) for r in old_universe if str(r.get("session")) == "core_night"], "market_confirmation_score", 0.20)

    # AB confirmation: exact same timestamp+symbol between A and B. If too sparse, also report a 30m-nearest diagnostic.
    b_exact_keys = {(r["ts_dt"], str(r.get("symbol"))) for r in B}
    AB_exact = [dict(r) for r in A if (r["ts_dt"], str(r.get("symbol"))) in b_exact_keys]
    # Nearest diagnostic: old B confirmation for same symbol within +/-30m.
    b_by_sym: defaultdict[str, list[datetime]] = defaultdict(list)
    for r in B:
        b_by_sym[str(r.get("symbol"))].append(r["ts_dt"])
    AB_near: list[dict[str, Any]] = []
    for r in A:
        sym = str(r.get("symbol"))
        ts = r["ts_dt"]
        if any(abs((ts - t).total_seconds()) <= 30 * 60 for t in b_by_sym.get(sym, [])):
            AB_near.append(dict(r))

    sets = {
        "A_new_market_top20": (new_universe, A),
        "B_old_watch_hot": (old_universe, B),
        "AB_exact_A_confirmed_by_B": (A, AB_exact),
        "AB_near30m_A_confirmed_by_B_DIAGNOSTIC": (A, AB_near),
        "C_old_core_night_market_top20": (old_universe, C),
    }
    result: dict[str, Any] = {
        "generated_utc": iso(now_utc),
        "generated_bjt": now_utc.astimezone(BJ).isoformat(),
        "method": "Tonight first-layer discovery only: signal -> next complete 15m Binance USDT-M open; horizons 15m/30m/1h close_return and MFE; same-timestamp random p95. No managed_1h trading/execution replay.",
        "window": {"tonight_start_bjt": TONIGHT_START_BJT.isoformat(), "tonight_start_utc": iso(TONIGHT_START_UTC)},
        "meta": {"new_meta": new_meta, "new_path_meta": new_path_meta, "old_meta": old_meta, "old_path_meta": old_path_meta, "counts": {"new_universe": len(new_universe), "old_universe": len(old_universe), "A": len(A), "B": len(B), "AB_exact": len(AB_exact), "AB_near30m": len(AB_near), "C": len(C)}},
        "sets": {},
    }
    lines = [
        "# Tonight first-layer discovery validation",
        "",
        f"generated_bjt: `{result['generated_bjt']}`",
        "",
        result["method"],
        "",
        "## Coverage",
        "```text",
        f"tonight_start_bjt={TONIGHT_START_BJT.isoformat()}",
        f"new_latest_snapshot_utc={new_meta.get('latest_snapshot_utc')} new_complete_end_utc={new_meta.get('complete_end_utc')} new_path_rows={len(new_path)} new_tonight_universe={len(new_universe)}",
        f"old_max_ts={old_meta.get('max_ts')} old_complete_1h={old_meta.get('complete_cutoffs',{}).get('1h')} old_tonight_universe={len(old_universe)}",
        f"counts A={len(A)} B={len(B)} AB_exact={len(AB_exact)} AB_near30m={len(AB_near)} C={len(C)}",
        "```",
        "",
        "## Pass summary",
        "```text",
    ]
    for idx, (name, (universe, rows)) in enumerate(sets.items()):
        block = summarize(name, universe, rows, seed=2026060300 + idx * 1000)
        result["sets"][name] = block
        lines.append(line(name, block))
    lines.extend(["```", ""])
    for name, block in result["sets"].items():
        lines.extend(details_lines(name, block))

    out = PROJECT_ROOT / "output" / f"tonight-first-layer-discovery-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
