"""Jito Block Engine client for anti-sandwich MEV bundle submission."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.exceptions import APIError

logger = logging.getLogger(__name__)

# Known Jito tip accounts on Solana mainnet
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTxvQssQn138i2NN7MQ6624thew5VpownL3q",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgqP2DTwuzgWhbGMWCh36hWFvNNoU",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


class JitoClient:
    """Sends atomic bundles to Jito Block Engine to prevent sandwich attacks."""

    def __init__(
        self,
        block_engine_url: str = "https://mainnet.block-engine.jito.wtf",
        tip_lamports: int = 10000,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.block_engine_url = block_engine_url.rstrip("/")
        self.tip_lamports = tip_lamports
        self.timeout = timeout
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    def get_tip_account(self) -> str:
        """Return the primary Jito tip recipient account."""
        return JITO_TIP_ACCOUNTS[0]

    async def send_bundle(self, encoded_transactions: list[str]) -> str:
        """
        Send a bundle of base58/base64 encoded transactions directly to Jito validators.

        Returns
        -------
        Bundle ID string.
        """
        url = f"{self.block_engine_url}/api/v1/bundles"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [encoded_transactions],
        }

        resp = await self._client.post(url, json=payload)
        if resp.status_code != 200:
            raise APIError("Jito", f"Failed to send bundle: {resp.text}", resp.status_code)

        data = resp.json()
        bundle_id = data.get("result")
        if not bundle_id:
            raise APIError("Jito", f"Bundle rejected: {data.get('error')}")

        logger.info("Jito bundle submitted successfully [bundle_id=%s]", bundle_id)
        return bundle_id
