"""Helius / Solana WebSocket listener for instant detection of new Raydium and Pump.fun pools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable, Coroutine, Any

import websockets

from src.core.enums import TokenSource, DexType
from src.core.models import TokenSnapshot
from src.detection.snapshot import build_token_snapshot

logger = logging.getLogger(__name__)

# Well-known Solana Program IDs
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Base58 public key pattern (32 to 44 characters)
BASE58_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


class HeliusWebSocketClient:
    """Listens for new pool creations via Solana logsSubscribe WebSocket."""

    def __init__(
        self,
        ws_url: str = "",
        api_key: str = "",
        on_token_detected: Callable[[TokenSnapshot], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.api_key = api_key
        # Assemble WS URL if API key provided and URL is standard template
        if ws_url and api_key and "api-key=" in ws_url and not ws_url.endswith(api_key):
            self.ws_url = f"{ws_url}{api_key}" if ws_url.endswith("=") else f"{ws_url}{api_key}"
        elif ws_url:
            self.ws_url = ws_url
        elif api_key:
            self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        else:
            self.ws_url = ""

        self.on_token_detected = on_token_detected
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.ws_url)

    async def start(self) -> None:
        """Start WebSocket listener task if configured."""
        if not self.is_configured:
            logger.info("Helius WebSocket not configured (missing URL/API key), skipping WS stream.")
            return

        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("Helius WebSocket listener started: %s", self.ws_url.split("?")[0])

    async def stop(self) -> None:
        """Stop WebSocket listener task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Helius WebSocket listener stopped.")

    async def _listen_loop(self) -> None:
        """Main connection loop with auto-reconnect and exponential backoff."""
        backoff = 2
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=30, ping_timeout=10) as ws:
                    logger.info("Connected to Helius WebSocket. Subscribing to Raydium & Pump.fun logs...")
                    await self._subscribe(ws)
                    backoff = 2  # Reset on successful connection

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("WebSocket error: %s. Reconnecting in %ds...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Subscribe to logs from Raydium and Pump.fun programs."""
        # Subscribe to Raydium AMM v4
        sub_raydium = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [RAYDIUM_AMM_V4]},
                {"commitment": "confirmed"}
            ]
        }
        await ws.send(json.dumps(sub_raydium))

        # Subscribe to Pump.fun
        sub_pump = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_PROGRAM]},
                {"commitment": "confirmed"}
            ]
        }
        await ws.send(json.dumps(sub_pump))

    async def _handle_message(self, raw_msg: str | bytes) -> None:
        """Parse incoming log notification and extract token mint."""
        try:
            data = json.loads(raw_msg)
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            logs = value.get("logs", [])
            signature = value.get("signature")

            # Check if this log represents an initialization
            is_raydium_init = any("initialize2" in log.lower() or "init_pc_amount" in log for log in logs)
            is_pump_create = any("instruction: create" in log.lower() for log in logs)

            if not (is_raydium_init or is_pump_create):
                return

            dex = DexType.RAYDIUM if is_raydium_init else DexType.PUMPFUN
            logger.info("Detected new %s pool event in tx: %s", dex.value, signature)

            # Extract candidate addresses from logs
            all_text = " ".join(logs)
            candidates = BASE58_REGEX.findall(all_text)

            # Filter out known program IDs and standard sysvars
            known_ids = {
                RAYDIUM_AMM_V4,
                RAYDIUM_CPMM,
                PUMPFUN_PROGRAM,
                "11111111111111111111111111111111",
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "SysvarRent111111111111111111111111111111111",
                "So11111111111111111111111111111111111111112",  # WSOL
            }
            token_mints = [c for c in candidates if c not in known_ids and len(c) >= 32]

            if token_mints and self.on_token_detected:
                # First non-system address in creation log is typically the base token mint
                mint = token_mints[0]
                snapshot = build_token_snapshot(
                    mint_address=mint,
                    source=TokenSource.HELIUS_WS,
                    dex=dex,
                    token_age_seconds=0,
                    raw_metadata={"signature": signature, "logs": logs},
                )
                await self.on_token_detected(snapshot)

        except Exception as e:
            logger.debug("Failed to handle WebSocket message: %s", e)
