"""DexScreener API client for discovering new and trending Solana meme coins."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.core.enums import TokenSource, DexType
from src.core.models import TokenSnapshot
from src.detection.snapshot import build_token_snapshot

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SolanaMemeBot/1.0",
}


class DexScreenerClient:
    """Async HTTP client for DexScreener public API with batching & 429 rate-limit protection."""

    def __init__(
        self,
        base_url: str = "https://api.dexscreener.com",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        cleaned_url = base_url.rstrip("/")
        if cleaned_url.endswith("/latest"):
            cleaned_url = cleaned_url[: -len("/latest")]
        self.base_url = cleaned_url
        self.timeout = timeout
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout, headers=DEFAULT_HEADERS
        )
        self._rate_limit_until: float = 0.0
        self._profiles_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._boosts_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._top_boosts_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._search_idx: int = 0
        self._search_queries: tuple[str, ...] = (
            "pump",
            "pumpswap",
            "raydium",
            "solana",
            "meme",
            "ai",
            "cat",
            "dog",
            "pepe",
            "agent",
        )

    async def close(self) -> None:
        """Close HTTP client if created internally."""
        if not self._external_client:
            await self._client.aclose()

    def _is_cooling_down(self) -> bool:
        return time.monotonic() < self._rate_limit_until

    def _trigger_cooldown(self, seconds: float = 30.0) -> None:
        self._rate_limit_until = time.monotonic() + seconds

    async def get_latest_token_profiles(self) -> list[dict[str, Any]]:
        """Fetch recently updated/created token profiles on Solana (with 20s cache & 429 backoff)."""
        now = time.monotonic()
        if now - self._profiles_cache[0] < 20.0 and self._profiles_cache[1]:
            return self._profiles_cache[1]
        if self._is_cooling_down():
            return self._profiles_cache[1]

        url = f"{self.base_url}/token-profiles/latest/v1"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                logger.info("DexScreener rate-limited (429) on token-profiles; cooling down 30s")
                self._trigger_cooldown(30.0)
                return self._profiles_cache[1]
            if resp.status_code != 200:
                logger.warning("DexScreener get_latest_token_profiles returned HTTP %d", resp.status_code)
                return self._profiles_cache[1]
            data = resp.json()
            if isinstance(data, list):
                sol_items = [item for item in data if item.get("chainId") == "solana"]
                self._profiles_cache = (now, sol_items)
                return sol_items
            return []
        except Exception as e:
            logger.error("Error fetching DexScreener token profiles: %s", e)
            return self._profiles_cache[1]

    async def get_latest_boosted_tokens(self) -> list[dict[str, Any]]:
        """Fetch recently boosted tokens on Solana (with 25s cache & 429 backoff)."""
        now = time.monotonic()
        if now - self._boosts_cache[0] < 25.0 and self._boosts_cache[1]:
            return self._boosts_cache[1]
        if self._is_cooling_down():
            return self._boosts_cache[1]

        url = f"{self.base_url}/token-boosts/latest/v1"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                self._trigger_cooldown(30.0)
                return self._boosts_cache[1]
            if resp.status_code != 200:
                return self._boosts_cache[1]
            data = resp.json()
            if isinstance(data, list):
                sol_items = [item for item in data if item.get("chainId") == "solana"]
                self._boosts_cache = (now, sol_items)
                return sol_items
            return []
        except Exception as e:
            logger.error("Error fetching DexScreener boosted tokens: %s", e)
            return self._boosts_cache[1]

    async def get_top_boosted_tokens(self) -> list[dict[str, Any]]:
        """Fetch top active boosted tokens on Solana (with 35s cache)."""
        now = time.monotonic()
        if now - self._top_boosts_cache[0] < 35.0 and self._top_boosts_cache[1]:
            return self._top_boosts_cache[1]
        if self._is_cooling_down():
            return self._top_boosts_cache[1]

        url = f"{self.base_url}/token-boosts/top/v1"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                self._trigger_cooldown(30.0)
                return self._top_boosts_cache[1]
            if resp.status_code != 200:
                return self._top_boosts_cache[1]
            data = resp.json()
            if isinstance(data, list):
                sol_items = [item for item in data if item.get("chainId") == "solana"]
                self._top_boosts_cache = (now, sol_items)
                return sol_items
            return []
        except Exception as e:
            logger.error("Error fetching DexScreener top boosted tokens: %s", e)
            return self._top_boosts_cache[1]

    async def search_active_solana_pairs(self) -> list[dict[str, Any]]:
        """
        Rotate through active Solana DEX search queries (`/latest/dex/search?q=...`)
        which returns up to 30 full pair objects in a single call without needing a second lookup.
        """
        if self._is_cooling_down():
            return []
        q = self._search_queries[self._search_idx % len(self._search_queries)]
        self._search_idx += 1
        url = f"{self.base_url}/latest/dex/search?q={q}"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                self._trigger_cooldown(30.0)
                return []
            if resp.status_code != 200:
                return []
            data = resp.json()
            pairs = data.get("pairs") or []
            return [p for p in pairs if p.get("chainId") == "solana"]
        except Exception as e:
            logger.error("Error searching DexScreener pairs (q=%s): %s", q, e)
            return []

    async def get_token_pairs(self, mint_address: str) -> list[dict[str, Any]]:
        """Fetch all liquidity pairs for a specific token mint."""
        if self._is_cooling_down():
            return []
        url = f"{self.base_url}/latest/dex/tokens/{mint_address}"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                self._trigger_cooldown(30.0)
                return []
            if resp.status_code != 200:
                return []
            data = resp.json()
            pairs = data.get("pairs") or []
            return [p for p in pairs if p.get("chainId") == "solana"]
        except Exception as e:
            logger.error("Error fetching pairs for mint %s: %s", mint_address, e)
            return []

    async def get_tokens_pairs_batch(self, mint_addresses: list[str]) -> list[dict[str, Any]]:
        """
        Fetch liquidity pairs for up to 30 token mints in a SINGLE HTTP request
        (`/latest/dex/tokens/mint1,mint2,...`) to avoid HTTP 429 rate limits.
        """
        if not mint_addresses or self._is_cooling_down():
            return []
        unique_mints = list(dict.fromkeys(m for m in mint_addresses if m))[:30]
        if not unique_mints:
            return []
        joined = ",".join(unique_mints)
        url = f"{self.base_url}/latest/dex/tokens/{joined}"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 429:
                logger.info("DexScreener batch tokens rate-limited (429); cooling down 30s")
                self._trigger_cooldown(30.0)
                return []
            if resp.status_code != 200:
                return []
            data = resp.json()
            pairs = data.get("pairs") or []
            return [p for p in pairs if p.get("chainId") == "solana"]
        except Exception as e:
            logger.error("Error fetching batch pairs: %s", e)
            return []

    def parse_pair_to_snapshot(self, pair: dict[str, Any]) -> TokenSnapshot | None:
        """Parse raw DexScreener pair JSON into a standardized TokenSnapshot."""
        try:
            base_token = pair.get("baseToken", {})
            mint = base_token.get("address")
            if not mint or len(mint) < 32:
                return None

            dex_id = (pair.get("dexId") or "").lower()
            if "pump" in dex_id:
                dex = DexType.PUMPFUN
            elif "raydium" in dex_id:
                dex = DexType.RAYDIUM
            elif "orca" in dex_id:
                dex = DexType.ORCA
            else:
                dex = None

            liquidity = pair.get("liquidity", {})
            liquidity_usd = float(liquidity.get("usd", 0.0)) if liquidity else None

            volume = pair.get("volume", {})
            volume_24h = float(volume.get("h24", 0.0)) if volume else None

            price_usd = float(pair.get("priceUsd", 0.0)) if pair.get("priceUsd") else None
            mcap = float(pair.get("fdv", 0.0)) if pair.get("fdv") else None

            txns = pair.get("txns", {}).get("h24", {})
            buy_count = int(txns.get("buys", 0)) if txns else None
            sell_count = int(txns.get("sells", 0)) if txns else None

            created_at_ms = pair.get("pairCreatedAt")

            return build_token_snapshot(
                mint_address=mint,
                source=TokenSource.DEXSCREENER,
                symbol=base_token.get("symbol"),
                name=base_token.get("name"),
                pair_address=pair.get("pairAddress"),
                dex=dex,
                pair_created_at_ms=created_at_ms,
                price_usd=price_usd,
                mcap_usd=mcap,
                liquidity_usd=liquidity_usd,
                volume_24h=volume_24h,
                buy_count=buy_count,
                sell_count=sell_count,
                raw_metadata={"pair": pair},
            )
        except Exception as e:
            logger.error("Failed to parse DexScreener pair: %s", e)
            return None
