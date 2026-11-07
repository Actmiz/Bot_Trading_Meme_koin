"""Unit tests for TransactionExecutor across PAPER and LIVE modes."""

import pytest

from src.core.config import BotConfig, ExecutionConfig
from src.core.enums import BotMode
from src.core.exceptions import LiveModeError, MissingEnvError
from src.execution.executor import TransactionExecutor


MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.mark.asyncio
async def test_paper_execution_buy_and_sell():
    config = BotConfig(mode=BotMode.PAPER)
    executor = TransactionExecutor(config=config, fail_simulation_prob=0.0)

    # 1. Test Buy Order Simulation
    buy_res = await executor.execute_buy(
        mint_address=MINT_SAMPLE,
        amount_sol=0.1,
        current_price_usd=0.000025,
        liquidity_usd=50000.0,
    )
    assert buy_res.success is True
    assert buy_res.method == "paper_sim"
    assert buy_res.amount_tokens > 0
    assert buy_res.amount_sol == 0.1
    assert buy_res.actual_slippage_pct > 0.0
    assert buy_res.fee_sol > 0.0
    assert buy_res.tx_signature.startswith("sim_buy_")

    # 2. Test Sell Order Simulation
    sell_res = await executor.execute_sell(
        mint_address=MINT_SAMPLE,
        token_amount=buy_res.amount_tokens,
        current_price_usd=buy_res.fill_price_usd,
        liquidity_usd=50000.0,
    )
    assert sell_res.success is True
    assert sell_res.method == "paper_sim"
    assert sell_res.amount_sol > 0.0
    assert sell_res.tx_signature.startswith("sim_sell_")

    await executor.close()


@pytest.mark.asyncio
async def test_paper_slippage_exceeded():
    # Set max slippage very low (1 bps = 0.01%) so it gets rejected
    config = BotConfig(
        mode=BotMode.PAPER,
        execution=ExecutionConfig(max_slippage_bps=1),
    )
    executor = TransactionExecutor(config=config, fail_simulation_prob=0.0)

    buy_res = await executor.execute_buy(
        mint_address=MINT_SAMPLE,
        amount_sol=0.1,
        current_price_usd=0.000025,
        liquidity_usd=50000.0,
    )
    assert buy_res.success is False
    assert "Slippage exceeded" in buy_res.error_message

    await executor.close()


@pytest.mark.asyncio
async def test_live_mode_safety_gate_lock():
    # If mode is LIVE but live_confirm_required is True -> must raise LiveModeError
    config = BotConfig(
        mode=BotMode.LIVE,
        live_confirm_required=True,
    )
    executor = TransactionExecutor(config=config)

    with pytest.raises(LiveModeError):
        await executor.execute_buy(
            mint_address=MINT_SAMPLE,
            amount_sol=0.1,
            current_price_usd=0.000025,
        )

    await executor.close()


@pytest.mark.asyncio
async def test_live_mode_requires_private_key():
    # If mode is LIVE and live_confirm_required is False but private key is missing -> must raise MissingEnvError
    config = BotConfig(
        mode=BotMode.LIVE,
        live_confirm_required=False,
    )
    executor = TransactionExecutor(config=config)

    with pytest.raises(MissingEnvError):
        await executor.execute_buy(
            mint_address=MINT_SAMPLE,
            amount_sol=0.1,
            current_price_usd=0.000025,
        )

    await executor.close()


@pytest.mark.asyncio
async def test_live_mode_fails_closed_without_signing():
    """Even when fully unlocked, LIVE must refuse to trade rather than fake a fill."""
    from src.core.config import EnvSecrets
    from src.core.exceptions import ExecutionError

    config = BotConfig(
        mode=BotMode.LIVE,
        live_confirm_required=False,
        secrets=EnvSecrets(wallet_private_key="dummy-not-a-real-key"),
    )
    executor = TransactionExecutor(config=config)

    with pytest.raises(ExecutionError):
        await executor.execute_buy(MINT_SAMPLE, 0.1, 0.000025)
    with pytest.raises(ExecutionError):
        await executor.execute_sell(MINT_SAMPLE, 1000.0, 0.000025)

    await executor.close()
