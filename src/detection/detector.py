"""TokenDetector orchestrator — coordinates REST polling and WebSocket streaming."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.models import TokenSnapshot
from src.detection.dexscreener import DexScreenerClient
from src.detection.birdeye import BirdeyeClient
from src.detection.helius_ws import HeliusWebSocketClient

logger = logging.getLogger(__name__)


class TokenDetector:
    """
    Orchestrates token discovery from DexScreener, Birdeye, and Helius WebSocket.
    Deduplicates candidates, records snapshots in database, and forwards
    candidates to downstream processing (Screening).
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        on_candidate_found: Callable[[TokenSnapshot], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.on_candidate_found = on_candidate_found

        self._seen_mints: set[str] = set()
        self._inspected_mints: set[str] = set()
        self._running = False
        self._poll_task: asyncio.Task | None = None

        # API clients
        self.dexscreener = DexScreenerClient(
            base_url=config.detection.dexscreener_base_url
        )
        self.birdeye = BirdeyeClient(
            api_key=config.secrets.birdeye_api_key,
            base_url=config.detection.birdeye_base_url,
        )
        self.ws_client = HeliusWebSocketClient(
            ws_url=config.secrets.helius_ws_url,
            api_key=config.secrets.helius_api_key,
            on_token_detected=self.process_candidate,
        )

    async def start(self) -> None:
        """Start polling and WebSocket detection loops."""
        self._running = True
        logger.info("Starting TokenDetector (poll_interval=%ds)...", self.config.detection.poll_interval_sec)

        # Start WebSocket if enabled
        if self.config.detection.helius_ws_enabled and self.ws_client.is_configured:
            await self.ws_client.start()

        # Start polling loop
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Gracefully stop all detection workers."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        await self.ws_client.stop()
        await self.dexscreener.close()
        await self.birdeye.close()
        logger.info("TokenDetector stopped.")

    async def _poll_loop(self) -> None:
        """Periodic polling of DexScreener and Birdeye."""
        while self._running:
            try:
                await self.poll_new_tokens()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in detection poll loop: %s", e)

            await asyncio.sleep(self.config.detection.poll_interval_sec)

    async def poll_new_tokens(self, max_candidates: int = 8) -> list[TokenSnapshot]:
        """Fetch candidates from polling sources using batched pair lookups to prevent HTTP 429."""
        discovered: list[TokenSnapshot] = []

        if not self.config.detection.dexscreener_enabled:
            return discovered

        # 1. Collect unseen candidate mints from profiles, latest boosts, and top boosts
        candidate_mints: list[str] = []
        profiles = await self.dexscreener.get_latest_token_profiles()
        for item in profiles:
            mint = item.get("tokenAddress")
            if mint and mint not in self._seen_mints and mint not in self._inspected_mints:
                candidate_mints.append(mint)

        if len(candidate_mints) < 15:
            boosted = await self.dexscreener.get_latest_boosted_tokens()
            for item in boosted:
                mint = item.get("tokenAddress")
                if mint and mint not in self._seen_mints and mint not in self._inspected_mints:
                    candidate_mints.append(mint)

        if len(candidate_mints) < 15:
            top_boosted = await self.dexscreener.get_top_boosted_tokens()
            for item in top_boosted:
                mint = item.get("tokenAddress")
                if mint and mint not in self._seen_mints and mint not in self._inspected_mints:
                    candidate_mints.append(mint)

        pairs: list[dict[str, Any]] = []
        if candidate_mints:
            # Batch fetch up to 30 mints in a SINGLE HTTP request
            batch_mints = list(dict.fromkeys(candidate_mints))[:30]
            for m in batch_mints:
                self._inspected_mints.add(m)
            pairs.extend(await self.dexscreener.get_tokens_pairs_batch(batch_mints))

        # 2. Supplement with active Solana DEX search pairs when profile/boost lists have few new tokens
        if len(candidate_mints) < 10:
            search_pairs = await self.dexscreener.search_active_solana_pairs()
            for sp in search_pairs:
                base_mint = (sp.get("baseToken") or {}).get("address")
                if base_mint and base_mint not in self._seen_mints:
                    pairs.append(sp)

        if not pairs:
            return discovered

        # 3. Group by mint and pick the pair with the highest USD liquidity so small pools don't shadow main pools
        best_pair_by_mint: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            if not mint or mint in self._seen_mints:
                continue
            liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            prev = best_pair_by_mint.get(mint)
            if prev is None or liq_usd > float((prev.get("liquidity") or {}).get("usd") or 0.0):
                best_pair_by_mint[mint] = pair

        for mint, pair in best_pair_by_mint.items():
            if len(discovered) >= max_candidates:
                break
            snapshot = self.dexscreener.parse_pair_to_snapshot(pair)
            if (
                snapshot
                and snapshot.mint_address not in self._seen_mints
                and (snapshot.initial_liquidity_usd or 0.0) >= self.config.detection.min_liquidity_usd
            ):
                processed = await self.process_candidate(snapshot)
                if processed:
                    discovered.append(processed)

        # Keep _inspected_mints bounded
        if len(self._inspected_mints) > 2000:
            self._inspected_mints.clear()

        return discovered

    async def process_candidate(self, snapshot: TokenSnapshot) -> TokenSnapshot | None:
        """
        Validate preliminary filters, deduplicate, save to DB, and notify consumer.
        """
        mint = snapshot.mint_address

        # Check in-memory deduplication
        if mint in self._seen_mints:
            return None
        self._seen_mints.add(mint)

        # Check database deduplication
        existing = await self.db.get_token_by_mint(mint)
        if existing:
            return None

        # Check preliminary filters (min liquidity, max age)
        filters = self.config.detection
        if snapshot.initial_liquidity_usd is not None:
            if snapshot.initial_liquidity_usd < filters.min_liquidity_usd:
                logger.debug("Token %s skipped: liquidity $%.0f < min $%.0f",
                             mint, snapshot.initial_liquidity_usd, filters.min_liquidity_usd)
                return None

        if snapshot.token_age_seconds is not None:
            if snapshot.token_age_seconds > filters.max_token_age_sec:
                logger.debug("Token %s skipped: age %ds > max %ds",
                             mint, snapshot.token_age_seconds, filters.max_token_age_sec)
                return None

        # Enrich with Birdeye if missing holders/volume and API key available
        if self.birdeye.api_key and (snapshot.initial_holders is None or snapshot.initial_volume_24h is None):
            overview = await self.birdeye.get_token_overview(mint)
            if overview:
                if snapshot.initial_holders is None and overview.get("holder"):
                    snapshot.initial_holders = int(overview["holder"])
                if snapshot.initial_volume_24h is None and overview.get("v24hUSD"):
                    snapshot.initial_volume_24h = float(overview["v24hUSD"])

        # Persist snapshot to database
        token_id = await self.db.insert_token(snapshot)
        logger.info(
            "New candidate token registered [id=%d, symbol=%s, mint=%s, liq=$%.0f]",
            token_id, snapshot.symbol or "UNKNOWN", mint, snapshot.initial_liquidity_usd or 0.0
        )

        # Forward candidate to downstream listener
        if self.on_candidate_found:
            await self.on_candidate_found(snapshot)

        return snapshot
