"""Screening Gate 1: Mint & Freeze Authority Check.

Tokens where mint authority is not revoked can be inflated infinitely.
Tokens with freeze authority can lock users from transferring/selling (honeypot).
Both authorities MUST be revoked (None/null).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def check_authorities(
    mint_address: str,
    rpc_url: str,
    client: httpx.AsyncClient | None = None,
    parsed_account_data: dict[str, Any] | None = None,
) -> tuple[bool, bool, list[str]]:
    """
    Check if mint authority and freeze authority are revoked.

    Parameters
    ----------
    mint_address        : Solana mint public key.
    rpc_url             : Solana JSON-RPC endpoint.
    client              : Optional async HTTP client.
    parsed_account_data : Optional pre-fetched parsed account info (for testing/caching).

    Returns
    -------
    (mint_revoked, freeze_revoked, fail_reasons)
    """
    fail_reasons: list[str] = []

    # Use pre-supplied parsed data if available (e.g. from unit tests or bulk RPC)
    if parsed_account_data is None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                mint_address,
                {"encoding": "jsonParsed", "commitment": "confirmed"}
            ]
        }
        try:
            should_close = False
            if client is None:
                client = httpx.AsyncClient(timeout=10.0)
                should_close = True

            try:
                resp = await client.post(rpc_url, json=payload)
                data = resp.json()
                value = data.get("result", {}).get("value")
                if not value:
                    return False, False, [f"Account {mint_address} not found on RPC"]
                parsed_account_data = value.get("data", {}).get("parsed", {}).get("info", {})
            finally:
                if should_close:
                    await client.aclose()
        except Exception as e:
            logger.error("RPC error checking authorities for %s: %s", mint_address, e)
            return False, False, [f"RPC error checking authorities: {str(e)}"]

    mint_authority = parsed_account_data.get("mintAuthority")
    freeze_authority = parsed_account_data.get("freezeAuthority")

    mint_revoked = mint_authority is None
    freeze_revoked = freeze_authority is None

    if not mint_revoked:
        fail_reasons.append(f"Mint authority active (held by {mint_authority})")

    if not freeze_revoked:
        fail_reasons.append(f"Freeze authority active (held by {freeze_authority})")

    return mint_revoked, freeze_revoked, fail_reasons
