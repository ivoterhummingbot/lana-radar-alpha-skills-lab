from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceConfig:
    """Read-only source paths owned by the existing Lana research project."""

    source_root: Path
    maker_attention_db: Path
    community_history_db: Path
    source_output_dir: Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output"

DEFAULT_SOURCE = SourceConfig(
    source_root=Path("/Users/leon/Documents/Quant/lana-community-hotcoin-analyzer"),
    maker_attention_db=Path(
        "/Users/leon/Documents/Quant/lana-community-hotcoin-analyzer/output/maker_attention_radar/maker_attention_cron.sqlite"
    ),
    community_history_db=Path(
        "/Users/leon/Documents/Quant/lana-community-hotcoin-analyzer/output/lana_community/hourly-long-history.sqlite"
    ),
    source_output_dir=Path("/Users/leon/Documents/Quant/lana-community-hotcoin-analyzer/output/lana_community"),
)


ALPHAGBM_METHODS = {
    "bps_backtest": "signal/control walk-forward comparison",
    "fear_score": "fixed transparent multi-factor composite",
    "market_sentiment": "market regime and breadth gating",
    "take_profit": "exit-family lab and edge evaporation measurement",
    "watchlist_health": "shadow registry, alerts, thesis, stale/drift audit",
}
