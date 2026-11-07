"""Unit tests for Modul 1: Deteksi Token (DexScreener, Helius WS, Snapshot, Detector)."""

import json
import pytest
from unittest.mock import AsyncMock

from src.core.config import BotConfig, DetectionConfig
from src.core.database import Database
from src.core.enums import TokenSource, DexType
from src.core.models import TokenSnapshot
from src.detection.snapshot import build_token_snapshot
from src.detection.dexscreener import DexScreenerClient
from src.detection.helius_ws import HeliusWebSocketClient
from src.detection.detector import TokenDetector


# Sample DexScreener Pair payload
SAMPLE_PAIR = {
    "chainId": "solana",
    "dexId": "raydium",
    "url": "https://dexscreener.com/solana/abc123pair",
    "pairAddress": "Gh9ZwEmdLJ8DscKNTkTqPbNwLNNBjuSzaG9Vp2KGtKJr",
    "baseToken": {
        "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "name": "Bonk",
        "symbol": "BONK",
    },
    "quoteToken": {
        "address": "So11111111111111111111111111111111111111112",
        "symbol": "SOL",
    },
    "priceUsd": "0.000025",
    "fdv": 2500000.0,
    "liquidity": {"usd": 55000.0, "base": 1000000, "quote": 200},
    "volume": {"h24": 120000.0},
    "txns": {"h24": {"buys": 1500, "sells": 900}},
    "pairCreatedAt": 1700000000000,
}


def test_build_token_snapshot():
    snapshot = build_token_snapshot(
        mint_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        source=TokenSource.DEXSCREENER,
        symbol="BONK",
        name="Bonk",
        price_usd=0.000025,
        liquidity_usd=55000.0,
        volume_24h=120000.0,
        buy_count=1500,
        sell_count=900,
    )
    assert snapshot.mint_address == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert snapshot.symbol == "BONK"
    assert snapshot.initial_liquidity_usd == 55000.0
    assert snapshot.initial_buy_count == 1500


def test_dexscreener_parse_pair():
    client = DexScreenerClient()
    snapshot = client.parse_pair_to_snapshot(SAMPLE_PAIR)

    assert snapshot is not None
    assert snapshot.mint_address == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert snapshot.dex == DexType.RAYDIUM
    assert snapshot.symbol == "BONK"
    assert snapshot.initial_price_usd == 0.000025
    assert snapshot.initial_liquidity_usd == 55000.0
    assert snapshot.initial_volume_24h == 120000.0
    assert snapshot.initial_buy_count == 1500
    assert snapshot.initial_sell_count == 900


@pytest.mark.asyncio
async def test_detector_filters_and_deduplication():
    db = Database(db_path=":memory:")
    await db.connect()

    # Configure min liquidity = $10,000, max age = 3600s
    config = BotConfig(
        detection=DetectionConfig(min_liquidity_usd=10000, max_token_age_sec=3600)
    )

    forwarded_candidates = []

    async def on_candidate(snapshot: TokenSnapshot):
        forwarded_candidates.append(snapshot)

    detector = TokenDetector(config=config, database=db, on_candidate_found=on_candidate)

    # 1. Candidate below liquidity threshold -> should be rejected
    low_liq_snapshot = build_token_snapshot(
        mint_address="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        source=TokenSource.DEXSCREENER,
        symbol="LOWLIQ",
        liquidity_usd=3000.0,  # Below 10,000
    )
    res1 = await detector.process_candidate(low_liq_snapshot)
    assert res1 is None
    assert len(forwarded_candidates) == 0

    # 2. Candidate that passes all criteria -> should be accepted and saved to DB
    valid_snapshot = build_token_snapshot(
        mint_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        source=TokenSource.DEXSCREENER,
        symbol="BONK",
        liquidity_usd=50000.0,
        volume_24h=100000.0,
    )
    res2 = await detector.process_candidate(valid_snapshot)
    assert res2 is not None
    assert len(forwarded_candidates) == 1
    assert forwarded_candidates[0].mint_address == valid_snapshot.mint_address

    # Verify saved in SQLite database
    in_db = await db.get_token_by_mint(valid_snapshot.mint_address)
    assert in_db is not None
    assert in_db["symbol"] == "BONK"

    # 3. Duplicate candidate -> should be skipped
    res3 = await detector.process_candidate(valid_snapshot)
    assert res3 is None
    assert len(forwarded_candidates) == 1  # Still 1, no duplicate forwarded

    await db.close()


@pytest.mark.asyncio
async def test_helius_ws_log_parsing():
    detected_snapshots = []

    async def callback(s: TokenSnapshot):
        detected_snapshots.append(s)

    ws_client = HeliusWebSocketClient(
        ws_url="wss://mainnet.helius-rpc.com/?api-key=test",
        on_token_detected=callback,
    )

    # Simulated Raydium initialize2 log message
    simulated_ws_msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "value": {
                    "signature": "5UxN8WqH9vC...",
                    "logs": [
                        "Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 invoke [1]",
                        "Program log: initialize2: InitializeInstruction2",
                        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]",
                        "Program log: Instruction: InitializeMint",
                        "Program 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr success",
                        "Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 success"
                    ]
                }
            }
        }
    })

    await ws_client._handle_message(simulated_ws_msg)

    assert len(detected_snapshots) == 1
    snapshot = detected_snapshots[0]
    assert snapshot.source == TokenSource.HELIUS_WS
    assert snapshot.dex == DexType.RAYDIUM
    assert snapshot.mint_address == "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
