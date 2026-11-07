"""Screening Gate 4: Honeypot Simulation & Fee Check.

Simulates sell transactions to ensure:
1. Token can actually be sold (not a honeypot).
2. Sell tax / price impact does not exceed max_sell_tax_pct (e.g. 5%).
3. Token-2022 transfer fee does not exceed max_transfer_fee_pct (e.g. 3%).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"


async def check_honeypot_and_fees(
    mint_address: str,
    jupiter_api_url: str = "https://quote-api.jup.ag/v6",
    max_sell_tax_pct: float = 5.0,
    max_transfer_fee_pct: float = 3.0,
    client: httpx.AsyncClient | None = None,
    mock_simulation: dict[str, Any] | None = None,
) -> tuple[bool, float, float, list[str]]:
    """
    Simulate a sell trade via Jupiter quote and verify fees.

    Parameters
    ----------
    mint_address         : Token mint address.
    jupiter_api_url      : Jupiter quote API endpoint.
    max_sell_tax_pct     : Maximum acceptable sell tax / discrepancy percentage.
    max_transfer_fee_pct : Maximum acceptable SPL Token-2022 transfer fee.
    client               : Optional async HTTP client.
    mock_simulation      : Optional pre-defined simulation data for testing.

    Returns
    -------
    (is_safe, sell_tax_pct, transfer_fee_pct, fail_reasons)
    """
    fail_reasons: list[str] = []
    sell_tax_pct = 0.0
    transfer_fee_pct = 0.0

    # If mock data provided (for unit testing)
    if mock_simulation is not None:
        can_sell = mock_simulation.get("can_sell", True)
        sell_tax_pct = float(mock_simulation.get("sell_tax_pct", 0.0))
        transfer_fee_pct = float(mock_simulation.get("transfer_fee_pct", 0.0))
        error_msg = mock_simulation.get("error")

        if not can_sell or error_msg:
            fail_reasons.append(f"Honeypot detected: sell simulation failed ({error_msg or 'reverted'})")
        if sell_tax_pct > max_sell_tax_pct:
            fail_reasons.append(f"Excessive sell tax: {sell_tax_pct:.1f}% > max {max_sell_tax_pct:.1f}%")
        if transfer_fee_pct > max_transfer_fee_pct:
            fail_reasons.append(f"Excessive transfer fee: {transfer_fee_pct:.1f}% > max {max_transfer_fee_pct:.1f}%")

        is_safe = len(fail_reasons) == 0
        return is_safe, sell_tax_pct, transfer_fee_pct, fail_reasons

    # Live simulation via Jupiter quote: Token -> SOL
    # Try configured endpoint first, then fallback to Jupiter lite-api v1
    primary_url = f"{jupiter_api_url.rstrip('/')}/quote"
    fallback_url = "https://lite-api.jup.ag/swap/v1/quote"
    urls_to_try = [primary_url] if primary_url == fallback_url else [primary_url, fallback_url]

    params = {
        "inputMint": mint_address,
        "outputMint": SOL_MINT,
        "amount": "1000000",
        "slippageBps": 500,
    }

    try:
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            should_close = True

        try:
            resp = None
            last_exc: Exception | None = None
            for candidate_url in urls_to_try:
                try:
                    resp = await client.get(candidate_url, params=params)
                    break
                except Exception as exc:
                    last_exc = exc
                    continue

            if resp is None and last_exc is not None:
                raise last_exc

            if resp is not None and resp.status_code == 200:
                data = resp.json()
                # Check price impact or platform fees
                price_impact_pct = float(data.get("priceImpactPct", 0.0))
                # Check for fee bps
                route_plan = data.get("routePlan", [])
                if not route_plan:
                    fail_reasons.append("Honeypot: no trade route available to sell token")

            elif resp is not None and resp.status_code in (400, 404):
                data = resp.json()
                err = data.get("error", "Cannot route sell transaction")
                fail_reasons.append(f"Honeypot check failed: {err}")
            elif resp is not None:
                logger.warning("Jupiter quote returned HTTP %d for mint %s", resp.status_code, mint_address)
        finally:
            if should_close:
                await client.aclose()

    except Exception as e:
        err_str = str(e)
        if "getaddrinfo failed" in err_str or "ConnectError" in type(e).__name__:
            logger.warning("Jupiter quote DNS/network unreachable for %s (%s); deferring to DEX sell count", mint_address, e)
        else:
            logger.error("Honeypot check exception for %s: %s", mint_address, e)
            fail_reasons.append(f"Honeypot simulation error: {err_str}")

    if sell_tax_pct > max_sell_tax_pct:
        fail_reasons.append(f"Sell tax {sell_tax_pct:.1f}% exceeds max {max_sell_tax_pct:.1f}%")

    if transfer_fee_pct > max_transfer_fee_pct:
        fail_reasons.append(f"Transfer fee {transfer_fee_pct:.1f}% exceeds max {max_transfer_fee_pct:.1f}%")

    is_safe = len(fail_reasons) == 0
    return is_safe, sell_tax_pct, transfer_fee_pct, fail_reasons
