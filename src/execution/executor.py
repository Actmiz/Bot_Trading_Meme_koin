"""Transaction executor supporting PAPER, DEVNET, and LIVE modes."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

import httpx

from src.core.config import BotConfig
from src.core.enums import BotMode
from src.core.exceptions import (
    LiveModeError,
    ExecutionError,
    MissingEnvError,
    SlippageExceededError,
)
from src.execution.jupiter import JupiterClient, SOL_MINT
from src.execution.jito import JitoClient
from src.execution.priority_fee import estimate_priority_fee

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of an executed buy or sell order."""

    success: bool
    tx_signature: str | None
    fill_price_usd: float
    amount_tokens: float
    amount_sol: float
    actual_slippage_pct: float
    fee_sol: float
    method: str
    error_message: str | None = None


class TransactionExecutor:
    """
    Executes trades across PAPER, DEVNET, and LIVE environments.
    Guarantees strict safety checks before any real funds are transacted.
    """

    def __init__(
        self,
        config: BotConfig,
        http_client: httpx.AsyncClient | None = None,
        # Simulation flag for deterministic testing
        fail_simulation_prob: float = 0.0,
    ) -> None:
        self.config = config
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=15.0)
        self.fail_simulation_prob = fail_simulation_prob

        self.jupiter = JupiterClient(
            base_url=config.secrets.jupiter_api_url,
            http_client=self._client,
        )
        self.jito = JitoClient(
            block_engine_url=config.secrets.jito_block_engine_url,
            tip_lamports=config.execution.jito_tip_lamports,
            http_client=self._client,
        )

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def execute_buy(
        self,
        mint_address: str,
        amount_sol: float,
        current_price_usd: float,
        liquidity_usd: float | None = None,
    ) -> ExecutionResult:
        """
        Execute a buy order (SOL -> Token).
        """
        mode = self.config.mode

        if mode == BotMode.LIVE:
            return await self._execute_live_buy(mint_address, amount_sol, current_price_usd)
        elif mode == BotMode.DEVNET:
            return await self._execute_devnet_buy(mint_address, amount_sol, current_price_usd)
        else:
            return self._execute_paper_buy(mint_address, amount_sol, current_price_usd, liquidity_usd)

    async def execute_sell(
        self,
        mint_address: str,
        token_amount: float,
        current_price_usd: float,
        liquidity_usd: float | None = None,
    ) -> ExecutionResult:
        """
        Execute a sell order (Token -> SOL).
        """
        mode = self.config.mode

        if mode == BotMode.LIVE:
            return await self._execute_live_sell(mint_address, token_amount, current_price_usd)
        elif mode == BotMode.DEVNET:
            return await self._execute_devnet_sell(mint_address, token_amount, current_price_usd)
        else:
            return self._execute_paper_sell(mint_address, token_amount, current_price_usd, liquidity_usd)

    # -----------------------------------------------------------------------
    # PAPER MODE Ã¢â‚¬â€ Realistic Simulation
    # -----------------------------------------------------------------------
    def _execute_paper_buy(
        self,
        mint_address: str,
        amount_sol: float,
        current_price_usd: float,
        liquidity_usd: float | None = None,
    ) -> ExecutionResult:
        """
        Simulate realistic buy order:
        - Base slippage between 0.3% and 1.2%.
        - Size-based price impact = (Trade USD / Liquidity USD) * 100.
        - Network fee = 0.00005 SOL (base) + dynamic priority fee.
        - Occasional simulated transaction failure / timeout.
        """
        # Simulated failure check
        if random.random() < self.fail_simulation_prob:
            return ExecutionResult(
                success=False,
                tx_signature=None,
                fill_price_usd=current_price_usd,
                amount_tokens=0.0,
                amount_sol=amount_sol,
                actual_slippage_pct=0.0,
                fee_sol=0.00005,
                method="paper_sim",
                error_message="Simulated transaction drop/timeout on congested network",
            )

        trade_usd = amount_sol * self.config.execution.paper_sol_price_usd  # Approx SOL/USD reference
        liq = liquidity_usd or 20000.0
        price_impact_pct = (trade_usd / liq) * 50.0

        base_slippage_pct = random.uniform(0.3, 1.2)
        total_slippage_pct = round(base_slippage_pct + price_impact_pct, 3)

        # Slippage check
        max_allowed_slippage = self.config.execution.max_slippage_bps / 100.0  # e.g. 5.0%
        if total_slippage_pct > max_allowed_slippage:
            return ExecutionResult(
                success=False,
                tx_signature=None,
                fill_price_usd=current_price_usd,
                amount_tokens=0.0,
                amount_sol=amount_sol,
                actual_slippage_pct=total_slippage_pct,
                fee_sol=0.00005,
                method="paper_sim",
                error_message=f"Slippage exceeded: {total_slippage_pct:.2f}% > max {max_allowed_slippage:.2f}%",
            )

        fill_price_usd = current_price_usd * (1.0 + total_slippage_pct / 100.0)
        tokens_bought = (trade_usd / fill_price_usd) if fill_price_usd > 0 else 0.0
        fee_sol = round(0.00005 + random.uniform(0.00002, 0.00008), 6)

        sim_sig = f"sim_buy_{mint_address[:8]}_{int(random.random() * 1000000)}"

        logger.info(
            "[PAPER BUY] Mint=%s | Spent=%.4f SOL | Bought=%.2f tokens | Fill=$%.6f | Slippage=%.2f%%",
            mint_address, amount_sol, tokens_bought, fill_price_usd, total_slippage_pct
        )

        return ExecutionResult(
            success=True,
            tx_signature=sim_sig,
            fill_price_usd=fill_price_usd,
            amount_tokens=tokens_bought,
            amount_sol=amount_sol,
            actual_slippage_pct=total_slippage_pct,
            fee_sol=fee_sol,
            method="paper_sim",
        )

    def _execute_paper_sell(
        self,
        mint_address: str,
        token_amount: float,
        current_price_usd: float,
        liquidity_usd: float | None = None,
    ) -> ExecutionResult:
        """
        Simulate realistic sell order.
        """
        if random.random() < self.fail_simulation_prob:
            return ExecutionResult(
                success=False,
                tx_signature=None,
                fill_price_usd=current_price_usd,
                amount_tokens=token_amount,
                amount_sol=0.0,
                actual_slippage_pct=0.0,
                fee_sol=0.00005,
                method="paper_sim",
                error_message="Simulated transaction drop/timeout on congested network",
            )

        value_usd = token_amount * current_price_usd
        liq = liquidity_usd or 20000.0
        price_impact_pct = (value_usd / liq) * 50.0

        base_slippage_pct = random.uniform(0.3, 1.2)
        total_slippage_pct = round(base_slippage_pct + price_impact_pct, 3)

        # In a sell, slippage decreases received price
        fill_price_usd = current_price_usd * (1.0 - total_slippage_pct / 100.0)
        received_usd = token_amount * fill_price_usd
        received_sol = received_usd / self.config.execution.paper_sol_price_usd  # reference SOL/USD rate
        fee_sol = round(0.00005 + random.uniform(0.00002, 0.00008), 6)

        sim_sig = f"sim_sell_{mint_address[:8]}_{int(random.random() * 1000000)}"

        logger.info(
            "[PAPER SELL] Mint=%s | Sold=%.2f tokens | Received=%.4f SOL | Fill=$%.6f | Slippage=%.2f%%",
            mint_address, token_amount, received_sol, fill_price_usd, total_slippage_pct
        )

        return ExecutionResult(
            success=True,
            tx_signature=sim_sig,
            fill_price_usd=fill_price_usd,
            amount_tokens=token_amount,
            amount_sol=received_sol,
            actual_slippage_pct=total_slippage_pct,
            fee_sol=fee_sol,
            method="paper_sim",
        )


    # -----------------------------------------------------------------------
    # LIVE & DEVNET MODES â€” FAIL-CLOSED until on-chain signing is implemented
    # -----------------------------------------------------------------------
    # IMPORTANT: Real signing + sending (solders VersionedTransaction, RPC
    # sendTransaction / Jito sendBundle, and confirmation polling) is NOT yet
    # implemented. These paths deliberately refuse to trade rather than return
    # fabricated signatures, so a LIVE run can never record a fake fill.
    def _assert_live_allowed(self) -> None:
        """Hard safety gate for any real-funds path."""
        if self.config.live_confirm_required:
            raise LiveModeError(
                "LIVE trading is locked! live_confirm_required must be set to false explicitly."
            )
        if not self.config.secrets.wallet_private_key:
            raise MissingEnvError("WALLET_PRIVATE_KEY")

    async def _execute_live_buy(
        self, mint_address: str, amount_sol: float, current_price_usd: float
    ) -> ExecutionResult:
        self._assert_live_allowed()
        raise ExecutionError(
            "LIVE buy is not implemented yet (transaction signing/sending pending). "
            "Refusing to trade."
        )

    async def _execute_live_sell(
        self, mint_address: str, token_amount: float, current_price_usd: float
    ) -> ExecutionResult:
        self._assert_live_allowed()
        raise ExecutionError(
            "LIVE sell is not implemented yet (transaction signing/sending pending). "
            "Refusing to trade."
        )

    async def _execute_devnet_buy(
        self, mint_address: str, amount_sol: float, current_price_usd: float
    ) -> ExecutionResult:
        if not self.config.secrets.wallet_private_key:
            raise MissingEnvError("WALLET_PRIVATE_KEY")
        raise ExecutionError(
            "DEVNET buy is not implemented yet. Note: Jupiter has no devnet routing, so "
            "DEVNET mode will only test the signing/sending pipeline."
        )

    async def _execute_devnet_sell(
        self, mint_address: str, token_amount: float, current_price_usd: float
    ) -> ExecutionResult:
        if not self.config.secrets.wallet_private_key:
            raise MissingEnvError("WALLET_PRIVATE_KEY")
        raise ExecutionError("DEVNET sell is not implemented yet.")

