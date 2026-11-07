"""Jupiter v6 Swap API client."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.exceptions import APIError

logger = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterClient:
    """Async client for Jupiter v6 DEX aggregator."""

    def __init__(
        self,
        base_url: str = "https://quote-api.jup.ag/v6",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = 500,
    ) -> dict[str, Any]:
        """
        Fetch best swap quote from Jupiter.

        Parameters
        ----------
        input_mint      : Mint address being sold (e.g. SOL_MINT for buy).
        output_mint     : Mint address being purchased (token mint for buy).
        amount_lamports : Amount in atomic base units.
        slippage_bps    : Max slippage in basis points (500 = 5%).
        """
        url = f"{self.base_url}/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": slippage_bps,
        }

        resp = await self._client.get(url, params=params)
        if resp.status_code != 200:
            raise APIError("Jupiter", f"Quote failed: {resp.text}", resp.status_code)

        return resp.json()

    async def get_swap_transaction(
        self,
        quote_response: dict[str, Any],
        user_public_key: str,
        priority_fee_lamports: int = 10000,
        wrap_and_unwrap_sol: bool = True,
    ) -> str:
        """
        Get base64 serialized swap transaction from Jupiter.

        Returns
        -------
        Base64-encoded serialized transaction string.
        """
        url = f"{self.base_url}/swap"
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "computeUnitPriceMicroLamports": priority_fee_lamports,
        }

        resp = await self._client.post(url, json=payload)
        if resp.status_code != 200:
            raise APIError("Jupiter", f"Swap tx generation failed: {resp.text}", resp.status_code)

        data = resp.json()
        swap_tx = data.get("swapTransaction")
        if not swap_tx:
            raise APIError("Jupiter", "No swapTransaction returned in payload")

        return swap_tx
