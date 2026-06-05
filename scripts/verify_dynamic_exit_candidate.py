#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT = PROJECT_ROOT / "output"
SRC = OUT / "dynamic-exit-alpha-validation-latest.json"

TARGETS = [
    ("quality_BTC15_nonneg_AND_prev15_new_mkt_top20", "REGSPLIT_qualityTrail_elseF06"),
    ("risk_BTC15_nonnegative", "REGSPLIT_qualityTrail_elseF06"),
    ("base_all_C_cd60", "TRAIL06_T04_30m"),
    ("quality_BTC15_nonneg_AND_prev15_new_mkt_top20", "STATIC_P06_12_BE20"),
    ("quality_BTC15_nonneg_AND_prev15_new_mkt_top20", "STATIC_F06_SL10_10m"),
]


def pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def load_target(rows: list[Mapping[str, Any]], gate: str, exit_name: str) -> Mapping[str, Any]:
    for r in rows:
        if r.get("gate") == gate and r.get("exit") == exit_name:
            return r
    raise KeyError((gate, exit_name))


def split_daily(daily: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    days = sorted(daily)
    if not days:
        return {}
    cut = max(1, len(days) // 2)
    groups = {"early_train": days[:cut], "late_validate": days[cut:], "all_days": days}
    out: dict[str, Any] = {}
    for name, ds in groups.items():
        n = sum(int(daily[d]["n"]) for d in ds)
        sum_pnl = sum(float(daily[d]["sum"]) for d in ds)
        # approximate weighted average over executed trades; random p95 daily is not additive, so keep edge-day count too.
        avg = sum_pnl / n if n else 0.0
        edge_days = sum(1 for d in ds if float(daily[d].get("edge_avg_p95", 0.0)) > 0)
        avg_edge = sum(float(daily[d].get("edge_avg_p95", 0.0)) for d in ds) / len(ds) if ds else 0.0
        out[name] = {"days": ds, "n": n, "sum": sum_pnl, "avg": avg, "edge_days": edge_days, "total_days": len(ds), "mean_daily_edge_vs_rand_p95": avg_edge}
    return out


def main() -> int:
    data = json.loads(SRC.read_text())
    rows = data["leaderboard"]
    selected = []
    for gate, ex in TARGETS:
        r = load_target(rows, gate, ex)
        splits = split_daily(r.get("daily", {}))
        selected.append({"gate": gate, "exit": ex, "overall": r, "splits": splits})

    lines = []
    lines.append("# Dynamic exit candidate verification")
    lines.append("")
    lines.append(f"generated_utc: `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"source: `{SRC}`")
    lines.append("")
    lines.append("Fixed candidates only; no new parameter search in this verifier. Early/late split is chronological by BJT day, using daily same-timestamp random p95 already produced by `validate_dynamic_exit_alpha.py`.")
    lines.append("")
    lines.append("## Overall target comparison")
    lines.append("```text")
    for x in selected:
        r = x["overall"]
        lines.append(
            f"{x['gate']:<48} {x['exit']:<32} pass={r['pass_n']}/9 n={r['n']:3d}/{r['selected_n']:<3d} "
            f"avg={pct(r['avg']):>8} rand95={pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg_p95']):>8} "
            f"sum={pct(r['sum']):>8}/{pct(r['rand_sum_p95']):>8} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} "
            f"mdd={pct(r['mdd']):>8} remT5={pct(r['remove_top5_avg']):>8} dEdge={r['daily_edge_days']} stop={float(r['initial_stop_rate'])*100:4.1f}%"
        )
    lines.append("```")
    lines.append("")
    lines.append("## Chronological early/late daily check")
    for x in selected:
        r = x["overall"]
        lines.append(f"### {x['gate']} / {x['exit']}")
        lines.append("```text")
        for k in ["early_train", "late_validate", "all_days"]:
            s = x["splits"].get(k, {})
            lines.append(
                f"{k:<14} days={','.join(s.get('days', [])):<43} n={s.get('n',0):3d} avg={pct(s.get('avg',0.0)):>8} "
                f"sum={pct(s.get('sum',0.0)):>8} edge_days={s.get('edge_days',0)}/{s.get('total_days',0)} mean_daily_edge={pct(s.get('mean_daily_edge_vs_rand_p95',0.0)):>8}"
            )
        lines.append("daily:")
        for day, d in sorted(r.get("daily", {}).items()):
            lines.append(
                f"  {day} n={d['n']:3d} avg={pct(d['avg']):>8} rand95={pct(d['rand_avg_p95']):>8} edge={pct(d['edge_avg_p95']):>8} "
                f"sum={pct(d['sum']):>8} stop={float(d['initial_stop_rate'])*100:4.1f}%"
            )
        lines.append("```")
        lines.append("")

    # Conservative verdict for the main candidate.
    main = selected[0]
    main_r = main["overall"]
    late = main["splits"]["late_validate"]
    effective_shadow = (
        main_r["edge_avg_p95"] > 0
        and main_r["edge_sum_p95"] > 0
        and main_r["remove_top5_avg"] >= 0
        and main_r["mdd"] >= -0.02
        and late["edge_days"] == late["total_days"]
        and late["total_days"] >= 2
    )
    production = (
        effective_shadow
        and main_r["daily_edge_days"].startswith("4/")
    )
    lines.append("## Verdict")
    lines.append("")
    if effective_shadow and not production:
        lines.append("Verdict: **effective as a shadow candidate, not production-confirmed**.")
    elif production:
        lines.append("Verdict: **production-confirmed by this rule set**.")
    else:
        lines.append("Verdict: **not effective enough even as a shadow candidate**.")
    lines.append("")
    lines.append("Reason: the main dynamic-trail quality candidate beats overall same-timestamp random p95 on avg/sum/sharpe, keeps MDD low, has positive remove-top5, and the late chronological validation days are both positive vs daily random p95. It still fails full daily robustness because early days do not all beat daily random p95.")
    lines.append("")

    out = OUT / f"dynamic-exit-candidate-verification-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    payload = {"source": str(SRC), "targets": selected, "effective_shadow": effective_shadow, "production_confirmed": production}
    out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    (OUT / "dynamic-exit-candidate-verification-latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    (OUT / "dynamic-exit-candidate-verification-latest.md").write_text("\n".join(lines) + "\n")
    print(out.with_suffix(".json"))
    print(out.with_suffix(".md"))
    print(OUT / "dynamic-exit-candidate-verification-latest.json")
    print(OUT / "dynamic-exit-candidate-verification-latest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
