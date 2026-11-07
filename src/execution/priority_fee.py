"""Dynamic priority fee estimation for Solana transactions."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.config import ExecutionConfig

logger = logging.getLogger(__name__)


async def estimate_priority_fee(
    rpc_url: str,
    config: ExecutionConfig,
    account_keys: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """
    Estimate priority fee in micro-lamports per compute unit.

    Parameters
    ----------
    rpc_url      : Solana RPC URL.
    config       : ExecutionConfig (strategy: 'dynamic' or 'fixed').
    account_keys : Optional list of writable accounts for localized fee estimation.
    client       : Optional httpx.AsyncClient.

    Returns
    -------
    Priority fee in micro-lamports.
    """
    if config.priority_fee_strategy != "dynamic":
        return config.priority_fee_fixed_lamports

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getRecentPrioritizationFees",
        "params": [account_keys or []]
    }

    try:
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=5.0)
            should_close = True

        try:
            resp = await client.post(rpc_url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                fees = [item.get("prioritizationFee", 0) for item in data.get("result", [])]
                if fees:
                    fees.sort()
                    # Calculate configured percentile (e.g. 75th percentile)
                    idx = int((config.priority_fee_dynamic_percentile / 100.0) * len(fees))
                    idx = min(idx, len(fees) - 1)
                    chosen_fee = fees[idx]
                    # Clamp between 5,000 and 500,000 micro-lamports
                    return max(5000, min(500000, chosen_fee))
        finally:
            if should_close:
                await client.aclose()
    except Exception as e:
        logger.debug("Failed to fetch dynamic priority fees from RPC: %s", e)

    return config.priority_fee_fixed_lamports
