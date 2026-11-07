"""Feature extraction and normalization for token scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.core.models import TokenSnapshot


@dataclass(frozen=True)
class ScoringFeatures:
    """Normalized feature values in the range [0.0, 1.0]."""

    price_momentum: float
    volume_liquidity_ratio: float
    holder_growth: float
    buy_sell_ratio: float
    token_age: float

    def to_dict(self) -> dict[str, float]:
        return {
            "price_momentum": self.price_momentum,
            "volume_liquidity_ratio": self.volume_liquidity_ratio,
            "holder_growth": self.holder_growth,
            "buy_sell_ratio": self.buy_sell_ratio,
            "token_age": self.token_age,
        }


def _clamp(val: float, low: float = 0.0, high: float = 1.0) -> float:
    """Ensure value is bounded between low and high."""
    return max(low, min(high, val))


def extract_scoring_features(
    token: TokenSnapshot,
    price_change_5m_pct: float | None = None,
    holder_count: int | None = None,
    current_liquidity_usd: float | None = None,
    current_volume_usd: float | None = None,
) -> ScoringFeatures:
    """
    Extract and normalize features from token snapshot and current market data.
    All outputs are normalized to [0.0, 1.0] where 1.0 represents the highest/most favorable signal.

    Parameters
    ----------
    token                 : Base TokenSnapshot.
    price_change_5m_pct   : Recent price change % (e.g., +15%).
    holder_count          : Current or initial holder count.
    current_liquidity_usd : Current liquidity (falls back to snapshot).
    current_volume_usd    : Current volume (falls back to snapshot).

    Returns
    -------
    ScoringFeatures dataclass.
    """
    liq = current_liquidity_usd or token.initial_liquidity_usd or 10000.0
    vol = current_volume_usd or token.initial_volume_24h or 0.0
    buys = token.initial_buy_count or 0
    sells = token.initial_sell_count or 0
    if holder_count is not None:
        holders = holder_count
    elif token.initial_holders is not None:
        holders = token.initial_holders
    else:
        # When DexScreener does not provide holder count, estimate breadth from 24h tx activity & liquidity
        holders = max(10, int((buys + sells) * 0.45 + (liq / 600.0)))
    age_sec = token.token_age_seconds if token.token_age_seconds is not None else 600

    # -----------------------------------------------------------------------
    # 1. Price Momentum (Sigmoid / smooth mapping around 0% - 100% gain)
    # -----------------------------------------------------------------------
    # If price_change_5m_pct is available, use it. If not, inspect raw_metadata or default to neutral 0.5.
    if price_change_5m_pct is not None:
        # A 5m change of +20% or more gets high score; negative gets low score
        # e.g., -20% -> 0.1, 0% -> 0.5, +20% -> 0.73, +50% -> 0.92
        norm_momentum = 1.0 / (1.0 + math.exp(-price_change_5m_pct / 15.0))
    else:
        # Check metadata if DexScreener provided priceChange.m5
        meta = token.metadata_json or {}
        pair = meta.get("pair", {})
        price_change = pair.get("priceChange", {})
        m5 = float(price_change.get("m5", 0.0)) if price_change.get("m5") is not None else 0.0
        norm_momentum = 1.0 / (1.0 + math.exp(-m5 / 15.0))

    norm_momentum = _clamp(norm_momentum)

    # -----------------------------------------------------------------------
    # 2. Volume to Liquidity Ratio (V/L)
    # -----------------------------------------------------------------------
    # For hot meme tokens:
    # V/L < 0.2: dead/low interest -> ~0.2
    # V/L ~ 1.0: healthy -> ~0.6
    # V/L >= 3.0: high velocity -> 0.95+
    vl_ratio = vol / max(1000.0, liq)
    # Smooth saturation curve: vl_ratio / (vl_ratio + 1.5)
    norm_vl = _clamp(vl_ratio / (vl_ratio + 1.2))

    # -----------------------------------------------------------------------
    # 3. Holder Growth / Breadth
    # -----------------------------------------------------------------------
    # More distributed holders = harder to rug.
    # <50 holders: 0.2
    # 200 holders: 0.6
    # 500+ holders: 0.9+
    norm_holders = _clamp(math.log10(max(1, holders)) / 3.0)

    # -----------------------------------------------------------------------
    # 4. Buy / Sell Ratio
    # -----------------------------------------------------------------------
    # Total transactions:
    total_tx = buys + sells
    if total_tx > 0:
        # Buy percentage directly maps to [0, 1]
        buy_pct = buys / total_tx
        # Boost when buys significantly outnumber sells
        norm_bs = _clamp(buy_pct)
    else:
        norm_bs = 0.5

    # -----------------------------------------------------------------------
    # 5. Token Age Score
    # -----------------------------------------------------------------------
    # Sweet spot for entry: 3 minutes (180s) to 30 minutes (1800s).
    # < 60s: extreme sniper volatility -> 0.4
    # 180s - 1200s (3m - 20m): prime sweet spot -> 0.9 - 1.0
    # > 3600s (1h+): old news, momentum tapering -> drops to 0.3
    if age_sec < 60:
        norm_age = 0.4
    elif 60 <= age_sec <= 1800:
        # Peak score in sweet spot
        norm_age = 0.7 + 0.3 * (1.0 - abs(age_sec - 600) / 1200.0)
    elif 1800 < age_sec <= 21600:
        # Gradual decay up to 6 hours for graduated Raydium/PumpSwap tokens
        norm_age = 0.7 - 0.35 * ((age_sec - 1800) / 19800.0)
    else:
        norm_age = 0.35

    norm_age = _clamp(norm_age)

    return ScoringFeatures(
        price_momentum=round(norm_momentum, 4),
        volume_liquidity_ratio=round(norm_vl, 4),
        holder_growth=round(norm_holders, 4),
        buy_sell_ratio=round(norm_bs, 4),
        token_age=round(norm_age, 4),
    )
