"""Birdeye API client for token overview and trending meme tokens."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.enums import TokenSource
from src.core.models import TokenSnapshot
from src.detection.snapshot import build_token_snapshot

logger = logging.getLogger(__name__)


class BirdeyeClient:
    """Client for Birdeye Solana public and partner APIs."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://public-api.birdeye.so",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close internal HTTP client."""
        if not self._external_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "x-chain": "solana"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        return headers

    async def get_token_overview(self, mint_address: str) -> dict[str, Any] | None:
        """Fetch token metrics (price, liquidity, volume, holders) from Birdeye."""
        if not self.api_key:
            return None

        url = f"{self.base_url}/defi/token_overview?address={mint_address}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("data")
            return None
        except Exception as e:
            logger.debug("Birdeye token overview lookup failed for %s: %s", mint_address, e)
            return None

    def parse_overview_to_snapshot(
        self, mint_address: str, data: dict[str, Any]
    ) -> TokenSnapshot | None:
        """Convert Birdeye token overview data into a TokenSnapshot."""
        try:
            return build_token_snapshot(
                mint_address=mint_address,
                source=TokenSource.BIRDEYE,
                symbol=data.get("symbol"),
                name=data.get("name"),
                price_usd=float(data.get("price", 0.0)) if data.get("price") else None,
                mcap_usd=float(data.get("mc", 0.0)) if data.get("mc") else None,
                liquidity_usd=float(data.get("liquidity", 0.0)) if data.get("liquidity") else None,
                volume_24h=float(data.get("v24hUSD", 0.0)) if data.get("v24hUSD") else None,
                holders=int(data.get("holder", 0)) if data.get("holder") else None,
                buy_count=int(data.get("buy24h", 0)) if data.get("buy24h") else None,
                sell_count=int(data.get("sell24h", 0)) if data.get("sell24h") else None,
                raw_metadata={"birdeye": data},
            )
        except Exception as e:
            logger.error("Failed to parse Birdeye data for %s: %s", mint_address, e)
            return None
