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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
for _script_path in (SCRIPTS_ROOT, *(p for p in SCRIPTS_ROOT.iterdir() if p.is_dir())):
    _path = str(_script_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import search_execution_alpha_focused_v2 as v2  # noqa: E402
import validate_c_cd60_new_radar_gate as gate  # noqa: E402
import validate_c_oldcore_cd60_daily as daily_base  # noqa: E402
import search_new_radar_exit_alpha as newexit  # noqa: E402
from radar_alpha_skills_lab.config import DEFAULT_SOURCE  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, load_snapshot_rows, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))

# Focused but deeper than the prior approved runner. Keep sims moderate so it can run on demand.
v2.SIMS = 160
v2.CAP_PER_TS = 8
v2.SLIM_PER_TS = 12
POOL = "C_oldcore_cd60"


def _num(x: Any) -> float:
    try:
        v = float(x or 0.0)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def bjt_day(row: Mapping[str, Any]) -> str:
    dt = row.get("entry_dt") or row.get("ts_dt")
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone(BJ).date().isoformat()


def summarize_combo(
    gate_name: str,
    rows_exec: Sequence[Mapping[str, Any]],
    selected_n: int,
    universe_exec: Sequence[Mapping[str, Any]],
    seed: int,
) -> dict[str, Any]:
    summ = v2.base.summarize_rows(rows_exec)
    extra = v2.contribution_extra(rows_exec)
    rand = v2.random_same_ts(universe_exec, rows_exec, seed) if rows_exec else {}
    st = summ["stats"]
    c = summ["contribution"]
    reason_counts = extra.get("reason_counts", {})
    vals_by_day: dict[str, list[float]] = defaultdict(list)
    rows_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    univ_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows_exec:
        rows_by_day[bjt_day(r)].append(r)
        vals_by_day[bjt_day(r)].append(float(r["pnl"]))
    for r in universe_exec:
        univ_by_day[bjt_day(r)].append(r)
    daily: dict[str, Any] = {}
    for i, day in enumerate(sorted(rows_by_day)):
        rr = rows_by_day[day]
        dr = daily_base.random_same_ts_exec(univ_by_day.get(day, []), rr, seed + 5000 + i * 37, min(100, v2.SIMS))
        ds = daily_base.summarize(rr, len(rr), dr if dr else None)
        daily[day] = {
            "n": ds["n"],
            "avg": ds["avg"],
            "rand_avg_p95": ds.get("rand_avg_p95", 0.0),
            "edge_avg_p95": ds.get("edge_avg_p95", 0.0),
            "sum": ds["sum"],
            "mdd": ds["mdd"],
            "remove_top5_avg": ds["remove_top5_avg"],
            "initial_stop_rate": ds["initial_stop_rate"],
            "reason_counts": ds["reason_counts"],
        }
    passes = {
        "avg_gt_rand_p95": bool(rand and st["avg"] > rand["avg"]["p95"]),
        "sum_gt_rand_p95": bool(rand and st["sum"] > rand["sum"]["p95"]),
        "sh_gt_rand_p95": bool(rand and st["sharpe_like"] > rand["sharpe_like"]["p95"]),
        "cap5_pos": summ["cap5"]["comp"] > 0,
        "cap10_pos": summ["cap10"]["comp"] > 0,
        "remove_top5_avg_pos": c["remove_top5"]["avg"] >= 0,
        "days_all_pos": extra.get("positive_avg_days", 0) == extra.get("days", 0) and extra.get("days", 0) > 0,
        "initial_stop_le_22pct": extra.get("initial_stop_rate", 1.0) <= 0.22,
        "mdd_gt_minus5pct": summ["mdd"] >= -0.05,
    }
    return {
        "gate": gate_name,
        "n": summ["n"],
        "selected_n": selected_n,
        "exec_rate": summ["n"] / selected_n if selected_n else 0.0,
        "avg": st["avg"],
        "sum": st["sum"],
        "sh": st["sharpe_like"],
        "mdd": summ["mdd"],
        "cap5": summ["cap5"]["comp"],
        "cap10": summ["cap10"]["comp"],
        "rand_avg_p95": rand.get("avg", {}).get("p95", 0.0) if rand else 0.0,
        "rand_sum_p95": rand.get("sum", {}).get("p95", 0.0) if rand else 0.0,
        "rand_sh_p95": rand.get("sharpe_like", {}).get("p95", 0.0) if rand else 0.0,
        "edge_avg_p95": st["avg"] - (rand.get("avg", {}).get("p95", 0.0) if rand else 0.0),
        "edge_sum_p95": st["sum"] - (rand.get("sum", {}).get("p95", 0.0) if rand else 0.0),
        "remove_top5_avg": c["remove_top5"]["avg"],
        "days": f"{extra.get('positive_avg_days', 0)}/{extra.get('days', 0)}",
        "daily_positive_edge_days": sum(1 for d in daily.values() if d.get("edge_avg_p95", 0.0) > 0),
        "daily_days": len(daily),
        "initial_stop_rate": extra.get("initial_stop_rate", 0.0),
        "win_rate": extra.get("win_rate", 0.0),
        "tp2_rate": reason_counts.get("tp2_after_tp1", 0) / len(rows_exec) if rows_exec else 0.0,
        "lock_rate": reason_counts.get("lock_after_tp1", 0) / len(rows_exec) if rows_exec else 0.0,
        "time_rate": (reason_counts.get("time_exit", 0) + reason_counts.get("tp1_then_time", 0)) / len(rows_exec) if rows_exec else 0.0,
        "avg_mfe": summ["avg_mfe"],
        "avg_capture": summ["avg_capture_ratio"],
        "reason_counts": reason_counts,
        "reason_avg": extra.get("reason_avg", {}),
        "top_symbols": c.get("top_symbols", [])[:8],
        "passes": passes,
        "daily": daily,
    }


def compact(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: r[k]
        for k in [
            "gate", "n", "selected_n", "exec_rate", "avg", "rand_avg_p95", "edge_avg_p95", "sum", "rand_sum_p95",
            "sh", "rand_sh_p95", "mdd", "cap5", "cap10", "remove_top5_avg", "days", "daily_positive_edge_days",
            "daily_days", "initial_stop_rate", "win_rate", "tp2_rate", "lock_rate", "time_rate", "avg_mfe", "avg_capture",
            "reason_counts", "top_symbols", "passes",
        ]
    }


def main() -> int:
    pools, pool_meta = v2.build_pools()
    universe, selected = pools[POOL]
    new_rows, new_meta = load_snapshot_rows(source=DEFAULT_SOURCE, hours=None)
    selected_annotated, gate_meta = gate.annotate_with_new_radar(selected, new_rows)

    gates: dict[str, list[Mapping[str, Any]]] = {
        "base_all_C_cd60": list(selected_annotated),
        "risk_BTC15_nonnegative": [r for r in selected_annotated if _num(r.get("new_prev30_btc_15m_ret")) >= 0.0],
        "quality_BTC15_nonneg_AND_prev15_new_mkt_top20": [
            r for r in selected_annotated if _num(r.get("new_prev30_btc_15m_ret")) >= 0.0 and bool(r.get("new_prev15_mkt_top20"))
        ],
        "anti_chase_BTC15_nonneg_AND_not_prev60_mkt_top10": [
            r for r in selected_annotated if _num(r.get("new_prev30_btc_15m_ret")) >= 0.0 and not bool(r.get("new_prev60_mkt_top10"))
        ],
    }
    gates = {k: v for k, v in gates.items() if len(v) >= 40 or k == "quality_BTC15_nonneg_AND_prev15_new_mkt_top20"}

    entries = [
        v2.Entry2("PB05_w15", "pullback", pullback=0.005, watch_minutes=15),
        v2.Entry2("PB07_w20", "pullback", pullback=0.007, watch_minutes=20),
        v2.Entry2("PB10_w25", "pullback", pullback=0.010, watch_minutes=25),
    ]
    exits = [
        v2.base.ExitSpec("P04_08_BE_10m", tp1=0.004, tp2=0.008, tp1_size=0.5, lock=0.0, sl=-0.008, time_minutes=10),
        v2.base.ExitSpec("P04_08_SZ67_BE_10m", tp1=0.004, tp2=0.008, tp1_size=0.67, lock=0.0, sl=-0.008, time_minutes=10),
        v2.base.ExitSpec("F04_SL08_8m", tp1=0.004, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.008, time_minutes=8, full_tp=True),
        v2.base.ExitSpec("P05_10_BE_12m", tp1=0.005, tp2=0.010, tp1_size=0.5, lock=0.0, sl=-0.009, time_minutes=12),
        v2.base.ExitSpec("P06_12_BE_15m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=15),
        v2.base.ExitSpec("P06_12_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.0, sl=-0.010, time_minutes=20),
        v2.base.ExitSpec("P06_12_L01_20m", tp1=0.006, tp2=0.012, tp1_size=0.5, lock=0.001, sl=-0.010, time_minutes=20),
        v2.base.ExitSpec("P06_12_SZ67_BE_20m", tp1=0.006, tp2=0.012, tp1_size=0.67, lock=0.0, sl=-0.010, time_minutes=20),
        v2.base.ExitSpec("F06_SL10_10m", tp1=0.006, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.010, time_minutes=10, full_tp=True),
        v2.base.ExitSpec("P08_16_BE_25m", tp1=0.008, tp2=0.016, tp1_size=0.5, lock=0.0, sl=-0.012, time_minutes=25),
        v2.base.ExitSpec("P10_20_BE_30m", tp1=0.010, tp2=0.020, tp1_size=0.5, lock=0.0, sl=-0.015, time_minutes=30),
        v2.base.ExitSpec("F10_SL15_25m", tp1=0.010, tp2=None, tp1_size=1.0, lock=0.0, sl=-0.015, time_minutes=25, full_tp=True),
    ]

    all_rows: list[Mapping[str, Any]] = list(universe) + list(selected_annotated)
    for g in gates.values():
        all_rows.extend(g)
    bars, bars_meta = v2.base.load_bars_by_symbol(all_rows, max_minutes=95)

    path_diag = {
        e.name: newexit.mfe_timing(selected_annotated, bars, e, 45)
        for e in entries
    }

    result: dict[str, Any] = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "method": "Deep exit diagnostics for old C cd60 plus causal new-radar gates. Discovery frozen; entries/exit variants only; all-taker 8bp; same-ts execution random over ungated C universe.",
        "settings": {"sims": v2.SIMS, "cap_per_ts": v2.CAP_PER_TS, "slim_per_ts": v2.SLIM_PER_TS, "pool": POOL},
        "meta": {"pool": pool_meta, "new": new_meta, "gate": gate_meta, "bars": bars_meta, "gate_counts": {k: len(v) for k, v in gates.items()}},
        "path_diag": path_diag,
        "leaderboard": [],
        "by_gate": {},
    }

    combo_idx = 0
    for entry in entries:
        for exit_spec in exits:
            combo_idx += 1
            universe_exec, _ = v2.simulate_combo2(universe, bars, entry, exit_spec)
            if not universe_exec:
                continue
            for gi, (gate_name, sig_rows) in enumerate(gates.items()):
                exec_rows, misses = v2.simulate_combo2(sig_rows, bars, entry, exit_spec)
                if len(exec_rows) < 20:
                    continue
                s = summarize_combo(gate_name, exec_rows, len(sig_rows), universe_exec, 2026061700 + combo_idx * 71 + gi * 13)
                s["entry"] = entry.name
                s["exit"] = exit_spec.name
                s["missed_entry"] = dict(misses)
                s["pass_n"] = sum(1 for v in s["passes"].values() if v)
                result["leaderboard"].append(s)
                result["by_gate"].setdefault(gate_name, []).append(s)

    result["leaderboard"] = sorted(
        result["leaderboard"],
        key=lambda r: (r["pass_n"], r["edge_avg_p95"], r["avg"], r["remove_top5_avg"], -abs(r["mdd"])),
        reverse=True,
    )
    for k in list(result["by_gate"]):
        result["by_gate"][k] = sorted(
            result["by_gate"][k],
            key=lambda r: (r["pass_n"], r["edge_avg_p95"], r["avg"], r["remove_top5_avg"], -abs(r["mdd"])),
            reverse=True,
        )

    # Markdown report.
    lines: list[str] = []
    lines.append("# Deep exit strategy diagnostics")
    lines.append("")
    lines.append(f"generated_utc: `{result['generated_utc']}`")
    lines.append("")
    lines.append(result["method"])
    lines.append("")
    lines.append("## Coverage")
    lines.append("```text")
    c = pool_meta["counts"][POOL]
    lines.append(f"{POOL}: selected_cap={c['selected_cap']} slim_universe={c['slim_universe']} symbols={c['symbols']} ts={c['timestamps']}")
    lines.append(f"new_rows={new_meta.get('rows')} complete_end_utc={new_meta.get('complete_end_utc')} gate_counts={result['meta']['gate_counts']}")
    lines.append(f"bars_symbols={bars_meta.get('symbols')} bars_errors={len(bars_meta.get('errors', {}))}")
    lines.append("```")
    lines.append("")
    lines.append("## Path/MFE timing on old C selected")
    lines.append("```text")
    for en, d in path_diag.items():
        hit04 = d["first_hit"].get("0.004", {})
        hit06 = d["first_hit"].get("0.006", {})
        hit10 = d["first_hit"].get("0.01", {})
        lines.append(
            f"{en:<9} n={d['n_path']:3d} mfe_avg={pct(d['mfe_avg']):>8} mfe_p75={pct(d['mfe_p75']):>8} mae_avg={pct(d['mae_avg']):>8} "
            f"maxMFEmin p50/p75/p90={d['max_mfe_minute_p50']:.0f}/{d['max_mfe_minute_p75']:.0f}/{d['max_mfe_minute_p90']:.0f} "
            f"hit0.4={hit04.get('hit_rate',0)*100:4.1f}%@p50m{hit04.get('p50_min',0):.0f} "
            f"hit0.6={hit06.get('hit_rate',0)*100:4.1f}%@p50m{hit06.get('p50_min',0):.0f} "
            f"hit1.0={hit10.get('hit_rate',0)*100:4.1f}%@p50m{hit10.get('p50_min',0):.0f}"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Top candidates")
    lines.append("```text")
    for r in result["leaderboard"][:50]:
        lines.append(
            f"{r['gate']:<48} {r['entry']:<9} {r['exit']:<22} pass={r['pass_n']}/9 "
            f"n={r['n']:3d}/{r['selected_n']:<3d} avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} "
            f"sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} "
            f"mdd={pct(r['mdd']):>8} cap5={pct(r['cap5']):>8} cap10={pct(r['cap10']):>8} remT5={pct(r['remove_top5_avg']):>8} "
            f"dEdge={r['daily_positive_edge_days']}/{r['daily_days']} stop={r['initial_stop_rate']*100:4.1f}% tp2={r['tp2_rate']*100:4.1f}% lock={r['lock_rate']*100:4.1f}% capR={r['avg_capture']:.2f} miss={r.get('missed_entry',{})}"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Best by gate")
    for gate_name, rows in result["by_gate"].items():
        lines.append(f"### {gate_name}")
        lines.append("```text")
        for r in rows[:8]:
            lines.append(
                f"{r['entry']:<9} {r['exit']:<22} pass={r['pass_n']}/9 n={r['n']:3d}/{r['selected_n']:<3d} "
                f"avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} "
                f"sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f} mdd={pct(r['mdd']):>8} "
                f"remT5={pct(r['remove_top5_avg']):>8} dEdge={r['daily_positive_edge_days']}/{r['daily_days']} stop={r['initial_stop_rate']*100:4.1f}% reasons={r['reason_counts']}"
            )
        lines.append("```")
        lines.append("")
    lines.append("## Daily for top 5")
    for r in result["leaderboard"][:5]:
        lines.append(f"### {r['gate']} / {r['entry']} / {r['exit']}")
        lines.append("```text")
        for day, d in r["daily"].items():
            lines.append(
                f"{day} n={d['n']:3d} avg={pct(d['avg']):>8} rand95={pct(d['rand_avg_p95']):>8} edge={pct(d['edge_avg_p95']):>8} "
                f"sum={pct(d['sum']):>8} mdd={pct(d['mdd']):>8} remT5={pct(d['remove_top5_avg']):>8} stop={d['initial_stop_rate']*100:4.1f}% reasons={d['reason_counts']}"
            )
        lines.append("```")
        lines.append("")

    out = OUT / f"deep-exit-strategy-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    # Also keep latest for future sessions.
    (OUT / "deep-exit-strategy-diagnostics-latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    (OUT / "deep-exit-strategy-diagnostics-latest.md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    print(OUT / "deep-exit-strategy-diagnostics-latest.json")
    print(OUT / "deep-exit-strategy-diagnostics-latest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
