"""Snapshot builder and normalizer for newly detected tokens."""

from __future__ import annotations

import time
from typing import Any

from src.core.enums import TokenSource, DexType
from src.core.models import TokenSnapshot, utc_now_iso


def build_token_snapshot(
    mint_address: str,
    source: TokenSource,
    symbol: str | None = None,
    name: str | None = None,
    pair_address: str | None = None,
    dex: DexType | None = None,
    deployer_address: str | None = None,
    token_age_seconds: int | None = None,
    pair_created_at_ms: int | None = None,
    price_usd: float | None = None,
    mcap_usd: float | None = None,
    liquidity_usd: float | None = None,
    volume_24h: float | None = None,
    holders: int | None = None,
    buy_count: int | None = None,
    sell_count: int | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> TokenSnapshot:
    """
    Construct a standardized TokenSnapshot instance.
    Calculates token_age_seconds if pair_created_at_ms is supplied, or uses token_age_seconds directly.
    """
    if token_age_seconds is None and pair_created_at_ms:
        now_ms = int(time.time() * 1000)
        token_age_seconds = max(0, int((now_ms - pair_created_at_ms) / 1000))

    return TokenSnapshot(
        mint_address=mint_address,
        symbol=symbol,
        name=name,
        source=source,
        pair_address=pair_address,
        dex=dex,
        deployer_address=deployer_address,
        detected_at=utc_now_iso(),
        token_age_seconds=token_age_seconds,
        initial_price_usd=price_usd,
        initial_mcap_usd=mcap_usd,
        initial_liquidity_usd=liquidity_usd,
        initial_volume_24h=volume_24h,
        initial_holders=holders,
        initial_buy_count=buy_count,
        initial_sell_count=sell_count,
        metadata_json=raw_metadata,
    )
