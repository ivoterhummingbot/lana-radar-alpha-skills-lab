from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ALPHAGBM_METHODS, OUTPUT_DIR


@dataclass(frozen=True)
class BlueprintSection:
    title: str
    purpose: str
    inputs: list[str]
    outputs: list[str]
    promotion_gate: list[str]


def build_blueprint() -> list[BlueprintSection]:
    return [
        BlueprintSection(
            title="1. BPS-style Signal/Control Audit",
            purpose="Prove each radar signal beats same-time controls before treating it as alpha.",
            inputs=[
                "maker_attn_symbol_scores",
                "maker_attn_market_snapshots",
                "frozen signal definitions",
                "fee model: maker0/taker4bp/slip scenarios",
            ],
            outputs=[
                "signal vs random matched-N",
                "signal vs all-watch baseline",
                "signal vs score-top baseline",
                "ablation without prev5m filter",
                "cap5/cap20 portfolio comp and MDD",
            ],
            promotion_gate=[
                "signal avg > random p95",
                "cap portfolio remains positive after realistic fee model",
                "remove_top5_symbols does not collapse the edge",
                "result is not isolated to one BJT hour or one symbol",
            ],
        ),
        BlueprintSection(
            title="2. Market-sentiment-style Regime Gate",
            purpose="Separate true signal alpha from BTC/alt breadth beta.",
            inputs=[
                "btc_regime_state",
                "btc_relative_gate_permission",
                "alt_breadth_1h/4h/24h",
                "hot_count",
                "BJT session label",
            ],
            outputs=[
                "signal × regime matrix",
                "best/worst regime buckets",
                "drawdown reduction from each gate",
            ],
            promotion_gate=[
                "gate improves MDD or net comp without severe sample collapse",
                "gate is explainable before seeing outcomes",
                "gate works in fresh-forward windows",
            ],
        ),
        BlueprintSection(
            title="3. Take-Profit-style Exit Lab",
            purpose="Verify whether managed_1h is structurally best or sample-specific.",
            inputs=[
                "same frozen entries from signal/control stage",
                "1m OHLC path",
                "exit family definitions",
            ],
            outputs=[
                "hold_15m/30m/60m/120m ranking",
                "managed_15m vs managed_1h",
                "tp/sl/partial-take ranking",
                "edge_evaporation_rate",
                "same-bar stop-first conservative replay",
            ],
            promotion_gate=[
                "chosen exit beats adjacent horizons",
                "edge survives fee/slip stress",
                "edge does not require hindsight exit selection",
            ],
        ),
        BlueprintSection(
            title="4. FearScore-style Fixed Composite Search",
            purpose="Search new alpha via transparent fixed components, not unconstrained threshold tuning.",
            inputs=[
                "community_heat_score",
                "source_diversity / source_quality_score",
                "freshness_score",
                "attention_spread_score",
                "prev5m_confirmation_score",
                "warning_score / fomo_risk_score",
                "btc and upper-wick risk fields",
            ],
            outputs=[
                "component contribution report",
                "decile/tercile performance",
                "fixed-weight candidate list",
            ],
            promotion_gate=[
                "top decile beats middle/bottom monotonically or near-monotonically",
                "weights are frozen before external validation",
                "component story is stable across days/regimes",
            ],
        ),
        BlueprintSection(
            title="5. Watchlist/Health-check Shadow Registry",
            purpose="Prevent stale or drifted shadow lanes from being treated as live alpha.",
            inputs=[
                "all candidate lane reports",
                "fresh-forward validation windows",
                "kill conditions",
            ],
            outputs=[
                "shadow lane cards",
                "stale/drift flags",
                "next validation actions",
            ],
            promotion_gate=[
                "each lane has explicit layer: discovery vs execution",
                "each lane has fee/venue assumptions",
                "each lane has kill conditions and last validation timestamp",
            ],
        ),
    ]


def render_blueprint() -> str:
    lines = [
        "# AlphaGBM-method Radar Alpha Discovery Blueprint",
        "",
        "This blueprint adapts AlphaGBM/skills methods to Lana radar alpha research without calling AlphaGBM APIs.",
        "",
        "## Method mapping",
        "",
    ]
    for key, value in ALPHAGBM_METHODS.items():
        lines.append(f"- `{key}` → {value}")
    lines.append("")

    for section in build_blueprint():
        lines.extend([f"## {section.title}", "", section.purpose, "", "Inputs:"])
        lines.extend(f"- {x}" for x in section.inputs)
        lines.extend(["", "Outputs:"])
        lines.extend(f"- {x}" for x in section.outputs)
        lines.extend(["", "Promotion gate:"])
        lines.extend(f"- {x}" for x in section.promotion_gate)
        lines.append("")

    lines.extend(
        [
            "## Non-negotiable boundaries",
            "",
            "- Do not modify `lana-community-hotcoin-analyzer` from this lab.",
            "- Do not overwrite the existing cap5/cap20 new-radar shadows.",
            "- Do not merge discovery and execution metrics.",
            "- Do not report cumulative signal sum as account PnL.",
            "- Do not promote a candidate without fresh-forward verification.",
            "",
        ]
    )
    return "\n".join(lines)


def write_blueprint(output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "alpha-discovery-blueprint.md"
    out.write_text(render_blueprint())
    return out
