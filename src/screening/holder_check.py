"""Screening Gate 2: Holder Concentration Check.

Checks top holders using getTokenLargestAccounts.
Excludes liquidity pool / DEX vault accounts from the top holder calculation.
Fails if top 10 non-pool holders exceed top10_holder_max_pct (e.g. 50%).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def check_holder_concentration(
    mint_address: str,
    rpc_url: str,
    max_top10_pct: float = 50.0,
    pair_address: str | None = None,
    client: httpx.AsyncClient | None = None,
    mock_holders_data: list[dict[str, Any]] | None = None,
    total_supply: float | None = None,
) -> tuple[bool, float, list[str]]:
    """
    Check if top 10 holders (excluding pool accounts) own less than max_top10_pct.

    Parameters
    ----------
    mint_address      : Token mint public key.
    rpc_url           : Solana RPC URL.
    max_top10_pct     : Configured maximum combined percentage allowed for top 10.
    pair_address      : Known DEX pair address to exclude from holder concentration.
    client            : Optional httpx.AsyncClient.
    mock_holders_data : Optional pre-fetched holder list for testing.
    total_supply      : Optional total supply for percentage calculation.

    Returns
    -------
    (is_ok, top10_pct, fail_reasons)
    """
    fail_reasons: list[str] = []

    holders = mock_holders_data
    if holders is None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint_address, {"commitment": "confirmed"}]
        }
        try:
            should_close = False
            if client is None:
                client = httpx.AsyncClient(timeout=10.0)
                should_close = True

            try:
                resp = await client.post(rpc_url, json=payload)
                data = resp.json()
                holders = data.get("result", {}).get("value") or []

                # Also fetch total supply via getTokenSupply if holders were returned
                if holders and total_supply is None:
                    sup_payload = {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "getTokenSupply",
                        "params": [mint_address, {"commitment": "confirmed"}],
                    }
                    sup_resp = await client.post(rpc_url, json=sup_payload)
                    sup_val = sup_resp.json().get("result", {}).get("value", {})
                    if sup_val and sup_val.get("uiAmount"):
                        total_supply = float(sup_val["uiAmount"])

                # Fallback to RugCheck topHolders if public RPC rate-limited getTokenLargestAccounts
                if not holders:
                    rc_resp = await client.get(f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report")
                    if rc_resp.status_code == 200:
                        rc_data = rc_resp.json()
                        rc_holders = rc_data.get("topHolders") or []
                        known_accounts = rc_data.get("knownAccounts") or {}
                        # Filter out AMM / LP / Creator vault accounts marked in knownAccounts
                        non_amm_pct = 0.0
                        count = 0
                        for h in rc_holders:
                            addr = h.get("address", "")
                            owner = h.get("owner", "")
                            acct_info = known_accounts.get(addr) or known_accounts.get(owner) or {}
                            acct_type = (acct_info.get("type") or "").upper()
                            if pair_address and addr == pair_address:
                                continue
                            if acct_type in ("AMM", "LOCKER", "BURN", "EXCHANGE"):
                                continue
                            pct_val = float(h.get("pct", 0.0) or 0.0)
                            # Skip single bonding-curve / LP vault holding > 35% on its own
                            if count == 0 and pct_val > 35.0:
                                continue
                            non_amm_pct += pct_val
                            count += 1
                            if count >= 10:
                                break
                        if rc_holders:
                            is_ok = non_amm_pct <= max_top10_pct
                            if not is_ok:
                                fail_reasons.append(
                                    f"Top 10 holders control {non_amm_pct:.1f}% of supply (exceeds limit {max_top10_pct:.1f}%)"
                                )
                            return is_ok, round(non_amm_pct, 2), fail_reasons
            finally:
                if should_close:
                    await client.aclose()
        except Exception as e:
            logger.error("RPC error checking holder concentration for %s: %s", mint_address, e)
            return False, 100.0, [f"RPC error fetching holders: {str(e)}"]

    if not holders:
        return False, 100.0, ["No holder accounts found or token supply is 0"]

    # Calculate total supply if not provided
    if total_supply is None:
        # Sum of largest accounts or fetch supply
        total_supply = sum(float(h.get("uiAmount", 0.0) or 0.0) for h in holders)
        if total_supply <= 0:
            return False, 100.0, ["Total supply could not be determined"]

    # Filter out pool address if known (and in live mode, exclude primary AMM LP vault if > 30% of supply)
    non_pool_holders = []
    for idx, h in enumerate(holders):
        addr = h.get("address")
        if pair_address and addr == pair_address:
            continue
        amt = float(h.get("uiAmount", 0.0) or 0.0)
        if mock_holders_data is None and idx == 0 and total_supply > 0 and (amt / total_supply) > 0.30:
            continue
        non_pool_holders.append(amt)

    # Take top 10 non-pool holders
    top10_amount = sum(non_pool_holders[:10])
    top10_pct = (top10_amount / total_supply) * 100.0

    is_ok = top10_pct <= max_top10_pct
    if not is_ok:
        fail_reasons.append(
            f"Top 10 holders control {top10_pct:.1f}% of supply (exceeds limit {max_top10_pct:.1f}%)"
        )

    return is_ok, top10_pct, fail_reasons
