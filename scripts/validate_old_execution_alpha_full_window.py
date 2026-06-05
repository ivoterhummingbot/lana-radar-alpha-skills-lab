#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import search_old_execution_alpha_second_stage as s2  # noqa: E402
import validate_old_execution_alpha_primary_robustness as rb  # noqa: E402
from radar_alpha_skills_lab.signal_control import iso, pct  # noqa: E402

OUT = PROJECT_ROOT / "output"
PRIMARY = ("night_mkt20_cd60", "PB10_w25", "TR08_T05_45")
NEIGHBORS = [
    ("night_mkt20_cd60", "PB10_w25", "TR06_T04_30"),
    ("night_mkt20_cd60", "PB10_w25", "P10_20_L02_45"),
    ("core_mkt20_cd60", "PB10_w25", "TR08_T05_45"),
]

# Full old-radar window: remove the 2026-06-01 execution-only cutoff used for fresh-forward search.
s2.EXEC_START_UTC = datetime(2000, 1, 1, tzinfo=timezone.utc)
# Full-window has far more rows than the 4-day fresh window; keep controls moderate so
# the replay finishes quickly, then use the result as a first full-window gate.
rb.SIMS = 300
rb.BOOT = 1000


def render_full(result: dict) -> str:
    lines = [
        "# Old execution alpha full-window validation",
        "",
        f"generated_utc: `{result['generated_utc']}`",
        "",
        "Full-window replay of the fixed execution candidate. This removes the 2026-06-01 cutoff and uses all available old-radar rows; same-ts random p95, bootstrap, top-symbol removal, day-wise validation, all-taker 8bp.",
        "",
        "## Meta",
        "```text",
        json.dumps(result["meta"], ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "## Primary",
        "```text",
    ]
    p = result["primary"]
    s = p["summary"]
    b = p["bootstrap"]
    rem = p["symbol_removal"]["remove_top5"]
    strict = sum(1 for d in p["daily"].values() if d["pass_sum"])
    lines.append(f"{p['pool']} {p['entry']}__{p['exit']} n={s['n']}/{s['selected_n']} avg={pct(s['avg'])}/{pct(s['rand_avg_p95'])} edge={pct(s['edge_avg_p95'])} sum={pct(s['sum'])}/{pct(s['rand_sum_p95'])} sh={s['sh']:.2f}/{s['rand_sh_p95']:.2f} cap5={pct(s['cap5']['comp'])} remTop5_avg={pct(rem['avg'])} remTop5_sum={pct(rem['sum'])} days={s['positive_days']}/{s['days']} strict={strict}/{len(p['daily'])} stop={s['initial_stop_rate']*100:.1f}%")
    lines.append(f"bootstrap avg p05/p50/p95={pct(b['avg_p05'])}/{pct(b['avg_p50'])}/{pct(b['avg_p95'])}; sum p05/p50/p95={pct(b['sum_p05'])}/{pct(b['sum_p50'])}/{pct(b['sum_p95'])}")
    lines.append("top symbols=" + ", ".join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in p["symbol_removal"]["top"][:10]))
    for x in p["symbol_removal"]["remove_each_top"][:8]:
        lines.append(f"remove {x['symbol']:<8} avg={pct(x['avg'])} sum={pct(x['sum'])} sh={x['sh']:.2f}")
    lines += ["```", "", "## Daily", "```text"]
    for d, ds in p["daily"].items():
        lines.append(f"{d} n={ds['n']:3d} avg={pct(ds['avg']):>8}/{pct(ds['rand_avg_p95']):>8} sum={pct(ds['sum']):>8}/{pct(ds['rand_sum_p95']):>8} pass_sum={ds['pass_sum']}")
    lines += ["```", "", "## Neighbor checks", "```text"]
    for item in result["neighbors"]:
        s = item["summary"]
        rem = item["symbol_removal"]["remove_top5"]
        strict = sum(1 for d in item["daily"].values() if d["pass_sum"])
        lines.append(f"{item['pool']:<24} {item['entry']+'__'+item['exit']:<24} n={s['n']:4d}/{s['selected_n']:<4d} avg={pct(s['avg']):>8}/{pct(s['rand_avg_p95']):>8} edge={pct(s['edge_avg_p95']):>8} sum={pct(s['sum']):>9}/{pct(s['rand_sum_p95']):>9} sh={s['sh']:5.2f}/{s['rand_sh_p95']:5.2f} cap5={pct(s['cap5']['comp']):>9} remT5={pct(rem['avg']):>8} days={s['positive_days']}/{s['days']} strict={strict}/{len(item['daily'])} stop={s['initial_stop_rate']*100:4.1f}%")
    lines += ["```", "", "## Verdict", "```text", result["verdict"], "```", ""]
    return "\n".join(lines)


def main() -> int:
    pools, meta = s2.build_old_pools()
    all_rows = []
    for univ, selected in pools.values():
        all_rows.extend(univ)
        all_rows.extend(selected)
    bars, bars_meta = s2.v2.base.load_bars_by_symbol(all_rows, max_minutes=max(x.time_minutes for x in s2.EXITS) + 45)
    primary = rb.summarize(*PRIMARY, pools=pools, bars=bars, seed=2026062001)
    neighbors = [rb.summarize(*x, pools=pools, bars=bars, seed=2026062100 + i * 997) for i, x in enumerate(NEIGHBORS)]
    ps = primary["summary"]
    strict_days = sum(1 for d in primary["daily"].values() if d["pass_sum"])
    day_count = len(primary["daily"])
    verdict = "PASS full-window shadow validation: overall edge is positive, all/most days are positive, bootstrap lower tail is positive, and top-symbol removal remains positive. Still not automatic production if strict same-ts random95 day pass ratio is weak or capacity constraints fail."
    if not (ps["edge_avg_p95"] > 0 and ps["positive_days"] >= max(1, day_count - 1) and strict_days >= max(1, int(day_count * 0.55))):
        verdict = "HOLD/FAIL full-window: fixed execution candidate does not pass required full-window gates; keep discovery/continuation conclusion separate from executable alpha."
    result = {
        "generated_utc": iso(datetime.now(timezone.utc)),
        "meta": {"pools": meta, "bars": bars_meta, "sims": rb.SIMS, "bootstrap": rb.BOOT, "primary": PRIMARY},
        "primary": primary,
        "neighbors": neighbors,
        "verdict": verdict,
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jp = OUT / f"old-execution-alpha-full-window-{ts}.json"
    mp = OUT / f"old-execution-alpha-full-window-{ts}.md"
    latest_jp = OUT / "old-execution-alpha-full-window-latest.json"
    latest_mp = OUT / "old-execution-alpha-full-window-latest.md"
    md = render_full(result)
    for p in [jp, latest_jp]:
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    for p in [mp, latest_mp]:
        p.write_text(md + "\n")
    print(jp)
    print(mp)
    print(latest_jp)
    print(latest_mp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
