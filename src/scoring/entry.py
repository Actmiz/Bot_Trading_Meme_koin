"""Entry Engine — connects screening, scoring, position sizing, and trade execution."""

from __future__ import annotations

import logging
from typing import Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType, TradeStatus
from src.core.models import (
    TokenSnapshot,
    ScreeningResult,
    ScoringResult,
    TradeRecord,
    DecisionLog,
    utc_now_iso,
)
from src.scoring.scorer import TokenScorer
from src.execution.executor import TransactionExecutor

logger = logging.getLogger(__name__)


class EntryEngine:
    """
    Automated entry engine.
    Executes a buy trade ONLY IF:
    1. Screening passed (gate keras).
    2. Composite score >= scoring threshold.
    3. Trade execution succeeds.
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        scorer: TokenScorer | None = None,
        executor: TransactionExecutor | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.scorer = scorer or TokenScorer(config, database)
        self.executor = executor or TransactionExecutor(config)

    async def evaluate_and_enter(
        self,
        token: TokenSnapshot,
        token_id: int,
        screening_result: ScreeningResult,
        wallet_capital_sol: float = 10.0,
        price_change_5m_pct: float | None = None,
        available_cash_sol: float | None = None,
    ) -> TradeRecord | None:
        """
        Evaluate candidate and enter position if all criteria are satisfied.

        Parameters
        ----------
        token               : TokenSnapshot candidate.
        token_id            : SQLite tokens.id.
        screening_result    : ScreeningResult from Modul 2.
        wallet_capital_sol  : Total bot capital in SOL for position sizing.
        price_change_5m_pct : Optional short-term momentum data.

        Returns
        -------
        TradeRecord if position was entered, or None.
        """
        mint = token.mint_address

        # Gate 1: Must pass screening (and be persisted, so FK is real)
        if not screening_result.passed:
            logger.info("Entry skipped for %s: failed security screening", mint)
            return None
        if screening_result.id is None:
            raise ValueError("screening_result.id is missing — screening must be persisted first")

        # Gate 2: Must pass scoring
        scoring_result = await self.scorer.score_token(
            token=token,
            token_id=token_id,
            screening_id=screening_result.id,
            price_change_5m_pct=price_change_5m_pct,
        )

        if not scoring_result.above_threshold:
            logger.info(
                "Entry skipped for %s: score %.4f < threshold %.2f",
                mint, scoring_result.total_score, scoring_result.threshold_used
            )
            return None

        # -------------------------------------------------------------------
        # Position Sizing (Follows Current Live Equity & Available Cash)
        # -------------------------------------------------------------------
        pos_cfg = self.config.position
        free_cash = wallet_capital_sol if available_cash_sol is None else available_cash_sol
        if free_cash < 0.001 or wallet_capital_sol < 0.001:
            logger.info(
                "Entry skipped for %s: insufficient available equity/cash (equity=%.4f SOL, cash=%.4f SOL)",
                mint, wallet_capital_sol, free_cash,
            )
            return None

        pct_amount = (pos_cfg.max_position_pct / 100.0) * wallet_capital_sol
        entry_sol = min(pct_amount, pos_cfg.max_position_sol, free_cash)
        entry_sol = round(max(0.001, min(entry_sol, free_cash)), 6)

        current_price = token.initial_price_usd or 0.0001
        logger.info(
            "Targeting ENTRY for %s | Score=%.4f | Equity=%.4f SOL | Size=%.4f SOL ($%.2f / %.1f%%)",
            mint,
            scoring_result.total_score,
            wallet_capital_sol,
            entry_sol,
            entry_sol * self.config.execution.paper_sol_price_usd,
            (entry_sol / wallet_capital_sol * 100.0) if wallet_capital_sol > 0 else 0.0,
        )

        # -------------------------------------------------------------------
        # Execute Buy Order
        # -------------------------------------------------------------------
        exec_res = await self.executor.execute_buy(
            mint_address=mint,
            amount_sol=entry_sol,
            current_price_usd=current_price,
            liquidity_usd=token.initial_liquidity_usd,
        )

        if not exec_res.success:
            logger.error("Entry order failed for %s: %s", mint, exec_res.error_message)
            decision = DecisionLog(
                decision_type=DecisionType.ERROR,
                mint_address=mint,
                token_id=token_id,
                reason=f"Buy order execution failed: {exec_res.error_message}",
            )
            await self.db.insert_decision(decision)
            return None

        # -------------------------------------------------------------------
        # Record Trade in Database
        # -------------------------------------------------------------------
        trade = TradeRecord(
            token_id=token_id,
            scoring_id=scoring_result.id,
            mint_address=mint,
            mode=self.config.mode.value,
            entry_price_usd=exec_res.fill_price_usd,
            entry_amount_sol=exec_res.amount_sol,
            entry_amount_tokens=exec_res.amount_tokens,
            entry_slippage_pct=exec_res.actual_slippage_pct,
            entry_fee_sol=exec_res.fee_sol,
            entry_tx_signature=exec_res.tx_signature,
            entry_method=exec_res.method,
            entry_at=utc_now_iso(),
            status=TradeStatus.OPEN,
            entry_features_json={
                "features": {
                    "price_momentum": scoring_result.feat_price_momentum,
                    "volume_liquidity_ratio": scoring_result.feat_volume_liquidity_ratio,
                    "holder_growth": scoring_result.feat_holder_growth_rate,
                    "buy_sell_ratio": scoring_result.feat_buy_sell_ratio,
                    "token_age": scoring_result.feat_token_age_score,
                },
                "weights": scoring_result.weights_json,
                "raw": {
                    "liquidity_usd": token.initial_liquidity_usd,
                    "volume_24h": token.initial_volume_24h,
                    "holders": token.initial_holders,
                    "token_age_seconds": token.token_age_seconds,
                    "top10_holder_pct": screening_result.top10_holder_pct,
                    "price_change_5m_pct": price_change_5m_pct,
                },
            },
            entry_score=scoring_result.total_score,
        )

        trade_id = await self.db.insert_trade(trade)
        trade.id = trade_id

        # Log entry decision
        decision = DecisionLog(
            decision_type=DecisionType.ENTRY,
            mint_address=mint,
            token_id=token_id,
            trade_id=trade_id,
            reason=f"Score {scoring_result.total_score:.4f} >= {scoring_result.threshold_used:.2f}",
            details_json={
                "fill_price_usd": exec_res.fill_price_usd,
                "amount_sol": exec_res.amount_sol,
                "amount_tokens": exec_res.amount_tokens,
                "tx_signature": exec_res.tx_signature,
            },
        )
        await self.db.insert_decision(decision)

        logger.info(
            "POSITION ENTERED [Trade #%d] %s: %.2f tokens for %.4f SOL at $%.6f",
            trade_id, mint, exec_res.amount_tokens, exec_res.amount_sol, exec_res.fill_price_usd
        )

        return trade
