"""TokenScorer — computes weighted composite score and evaluates entry threshold."""

from __future__ import annotations

import logging
from typing import Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType
from src.core.models import TokenSnapshot, ScoringResult, DecisionLog
from src.scoring.features import extract_scoring_features, ScoringFeatures

logger = logging.getLogger(__name__)


class TokenScorer:
    """Calculates weighted scores for screened tokens."""

    def __init__(self, config: BotConfig, database: Database) -> None:
        self.config = config
        self.db = database

    async def score_token(
        self,
        token: TokenSnapshot,
        token_id: int,
        screening_id: int,
        override_features: ScoringFeatures | None = None,
        price_change_5m_pct: float | None = None,
    ) -> ScoringResult:
        """
        Extract features, compute weighted composite score, persist, and return result.
        """
        weights = self.config.scoring.weights
        threshold = self.config.scoring.threshold

        # Extract or use injected features
        features = override_features or extract_scoring_features(
            token=token,
            price_change_5m_pct=price_change_5m_pct,
        )

        # Compute weighted sum
        total_score = (
            weights.get("price_momentum", 0.25) * features.price_momentum
            + weights.get("volume_liquidity_ratio", 0.20) * features.volume_liquidity_ratio
            + weights.get("holder_growth", 0.20) * features.holder_growth
            + weights.get("buy_sell_ratio", 0.20) * features.buy_sell_ratio
            + weights.get("token_age", 0.15) * features.token_age
        )
        total_score = round(total_score, 4)
        above_threshold = total_score >= threshold

        result = ScoringResult(
            token_id=token_id,
            screening_id=screening_id,
            feat_price_momentum=features.price_momentum,
            feat_volume_liquidity_ratio=features.volume_liquidity_ratio,
            feat_holder_growth_rate=features.holder_growth,
            feat_buy_sell_ratio=features.buy_sell_ratio,
            feat_token_age_score=features.token_age,
            total_score=total_score,
            threshold_used=threshold,
            above_threshold=above_threshold,
            weights_version="v1.0",
            weights_json=weights,
        )

        # Save to database
        scoring_id = await self.db.insert_scoring(result)
        result.id = scoring_id

        if above_threshold:
            logger.info(
                "Token %s passed scoring with score %.4f >= threshold %.2f",
                token.mint_address, total_score, threshold
            )
        else:
            logger.info(
                "Token %s rejected by score: %.4f < threshold %.2f",
                token.mint_address, total_score, threshold
            )
            # Log skip decision to database
            decision = DecisionLog(
                decision_type=DecisionType.SKIP_SCORE,
                mint_address=token.mint_address,
                token_id=token_id,
                reason=f"Score {total_score:.4f} < threshold {threshold:.2f}",
                details_json={
                    "total_score": total_score,
                    "threshold": threshold,
                    "features": features.to_dict(),
                    "scoring_id": scoring_id,
                },
            )
            await self.db.insert_decision(decision)

        return result
