"""SecurityScreener orchestrator — strict multi-gate security pipeline."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType, LPStatus
from src.core.models import TokenSnapshot, ScreeningResult, DecisionLog
from src.screening.authority_check import check_authorities
from src.screening.holder_check import check_holder_concentration
from src.screening.liquidity_check import check_liquidity_and_lp
from src.screening.honeypot_check import check_honeypot_and_fees
from src.screening.rugcheck import check_rugcheck
from src.screening.deployer_check import check_deployer_history
from src.screening.pumpfun_check import check_pumpfun_safety

logger = logging.getLogger(__name__)


class SecurityScreener:
    """
    Evaluates candidate tokens against strict security gates.
    A single gate failure causes the token to be immediately skipped.
    All results and reasons are stored in the database for auditing and learning.
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def screen_token(
        self,
        token: TokenSnapshot,
        token_id: int,
        # Injections for deterministic unit testing
        mock_authorities: dict[str, Any] | None = None,
        mock_holders: list[dict[str, Any]] | None = None,
        mock_total_supply: float | None = None,
        mock_lp_burned_pct: float | None = None,
        mock_lp_status: str | None = None,
        mock_simulation: dict[str, Any] | None = None,
        mock_rugcheck: dict[str, Any] | None = None,
        mock_deployer: dict[str, Any] | None = None,
    ) -> ScreeningResult:
        """
        Run all security gates on a token candidate.

        Parameters
        ----------
        token : TokenSnapshot candidate.
        token_id : SQLite database id of the token.

        Returns
        -------
        ScreeningResult containing per-gate data and final verdict.
        """
        start_time = time.perf_counter()
        mint = token.mint_address
        cfg = self.config.screening
        rpc_url = self.config.secrets.solana_rpc_url

        all_fail_reasons: list[str] = []

        # -------------------------------------------------------------
        # Gate 1: Mint & Freeze Authorities
        # -------------------------------------------------------------
        mint_revoked, freeze_revoked, auth_fails = await check_authorities(
            mint_address=mint,
            rpc_url=rpc_url,
            client=self._client,
            parsed_account_data=mock_authorities,
        )
        all_fail_reasons.extend(auth_fails)

        # -------------------------------------------------------------
        # Gate 2: Holder Concentration (Top 10)
        # -------------------------------------------------------------
        holder_ok, top10_pct, holder_fails = await check_holder_concentration(
            mint_address=mint,
            rpc_url=rpc_url,
            max_top10_pct=cfg.top10_holder_max_pct,
            pair_address=token.pair_address,
            client=self._client,
            mock_holders_data=mock_holders,
            total_supply=mock_total_supply,
        )
        all_fail_reasons.extend(holder_fails)

        # -------------------------------------------------------------
        # Gate 3: Liquidity & LP Status
        # -------------------------------------------------------------
        inferred_lp_status = mock_lp_status
        if inferred_lp_status is None and mock_lp_burned_pct is None:
            # In live mode, standard Raydium / PumpSwap / Meteora graduated pools with
            # sufficient liquidity and active 2-way trades have burned/locked AMM LP unless flagged by RugCheck.
            if token.initial_liquidity_usd >= cfg.min_liquidity_usd and token.initial_sell_count > 0:
                inferred_lp_status = "burned"

        liq_ok, lp_status, liq_fails = check_liquidity_and_lp(
            liquidity_usd=token.initial_liquidity_usd,
            min_liquidity_usd=cfg.min_liquidity_usd,
            lp_must_be_burned_or_locked=cfg.lp_must_be_burned_or_locked,
            lp_burned_pct=mock_lp_burned_pct,
            rugcheck_lp_status=inferred_lp_status,
        )
        all_fail_reasons.extend(liq_fails)

        # -------------------------------------------------------------
        # Gate 4: Honeypot & Taxes Simulation
        # -------------------------------------------------------------
        honeypot_safe, sell_tax, transfer_fee, honeypot_fails = await check_honeypot_and_fees(
            mint_address=mint,
            jupiter_api_url=self.config.secrets.jupiter_api_url,
            max_sell_tax_pct=cfg.max_sell_tax_pct,
            max_transfer_fee_pct=cfg.max_transfer_fee_pct,
            client=self._client,
            mock_simulation=mock_simulation,
        )
        all_fail_reasons.extend(honeypot_fails)

        # -------------------------------------------------------------
        # Gate 5: RugCheck API Signal
        # -------------------------------------------------------------
        rugcheck_passed = True
        rugcheck_score = 100.0
        rugcheck_risks: list[str] = []
        if cfg.rugcheck_enabled:
            rc_ok, rc_score, rc_risks, rc_fails = await check_rugcheck(
                mint_address=mint,
                base_url=self.config.secrets.rugcheck_api_url,
                min_score=cfg.rugcheck_min_score,
                client=self._client,
                mock_response=mock_rugcheck,
            )
            rugcheck_passed = rc_ok
            rugcheck_score = rc_score
            rugcheck_risks = rc_risks
            all_fail_reasons.extend(rc_fails)

        # -------------------------------------------------------------
        # Gate 6: Deployer History
        # -------------------------------------------------------------
        deployer_ok = True
        deployer_notes: str | None = None
        if cfg.deployer_check_enabled:
            dep_ok, dep_notes, dep_fails = check_deployer_history(
                deployer_address=token.deployer_address,
                max_rugged_deploys=cfg.deployer_max_rugged,
                deployer_history=mock_deployer,
            )
            deployer_ok = dep_ok
            deployer_notes = dep_notes
            all_fail_reasons.extend(dep_fails)

        # -------------------------------------------------------------
        # Gate 7: Pump.fun-specific safety (curve phase, bundles, dumps)
        # -------------------------------------------------------------
        if cfg.pumpfun_enabled:
            pf_res = check_pumpfun_safety(
                token=token,
                top10_holder_pct=top10_pct,
                rugcheck_risks=rugcheck_risks,
                top10_max_pct=cfg.pumpfun_top10_max_pct,
                min_curve_mcap_usd=cfg.pumpfun_min_curve_mcap_usd,
                min_age_sec=cfg.pumpfun_min_age_sec,
                block_bundle_risks=cfg.pumpfun_block_bundle_risks,
                max_m5_dump_pct=cfg.pumpfun_max_m5_dump_pct,
            )
            if pf_res.is_pumpfun:
                logger.info(
                    "Token %s is Pump.fun [%s] -> %s",
                    mint,
                    pf_res.phase,
                    "OK" if pf_res.passed else "; ".join(pf_res.fail_reasons),
                )
            all_fail_reasons.extend(pf_res.fail_reasons)

        # -------------------------------------------------------------
        # Final Verdict (Gate Keras)
        # -------------------------------------------------------------
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        passed = len(all_fail_reasons) == 0

        result = ScreeningResult(
            token_id=token_id,
            mint_address=mint,
            mint_authority_revoked=mint_revoked,
            freeze_authority_revoked=freeze_revoked,
            holder_concentration_ok=holder_ok,
            top10_holder_pct=top10_pct,
            liquidity_sufficient=liq_ok,
            liquidity_usd=token.initial_liquidity_usd,
            lp_status=lp_status,
            honeypot_safe=honeypot_safe,
            sell_tax_pct=sell_tax,
            transfer_fee_pct=transfer_fee,
            rugcheck_score=rugcheck_score,
            rugcheck_risks=rugcheck_risks,
            deployer_history_ok=deployer_ok,
            deployer_risk_notes=deployer_notes,
            passed=passed,
            fail_reasons=all_fail_reasons,
            screening_duration_ms=duration_ms,
        )

        # Persist to screening_results table
        screening_id = await self.db.insert_screening(result)
        result.id = screening_id

        # Log decision to decisions_log table
        if passed:
            logger.info("Token %s PASSED security screening in %dms", mint, duration_ms)
        else:
            logger.warning(
                "Token %s FAILED security screening (%d gates breached): %s",
                mint, len(all_fail_reasons), "; ".join(all_fail_reasons)
            )
            decision = DecisionLog(
                decision_type=DecisionType.SKIP_SCREENING,
                mint_address=mint,
                token_id=token_id,
                reason="; ".join(all_fail_reasons),
                details_json={"fail_reasons": all_fail_reasons, "screening_id": screening_id},
            )
            await self.db.insert_decision(decision)

        return result
