"""Main Bot Orchestrator — integrates Modules 1 through 7 into a single trading pipeline."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from src.core.config import BotConfig, load_config
from src.core.database import Database
from src.core.enums import BotMode, RiskEventType
from src.core.exceptions import LiveModeError
from src.core.models import TokenSnapshot, TradeRecord
from src.detection.detector import TokenDetector
from src.execution.executor import TransactionExecutor
from src.learning.optimizer import ParameterOptimizer
from src.logging_.telegram import TelegramNotifier
from src.logging_.trade_logger import TradeAuditLogger, setup_logging
from src.position.manager import PositionManager
from src.risk.controller import RiskController
from src.scoring.entry import EntryEngine
from src.scoring.scorer import TokenScorer
from src.screening.screener import SecurityScreener

logger = logging.getLogger(__name__)

LIVE_CONFIRMATION_PHRASE = "SAYA PAHAM RISIKO LIVE SOLANA"


class MemeTradingBot:
    """
    Top-level orchestrator connecting all 7 modules:
    1. TokenDetector (Modul 1)
    2. SecurityScreener (Modul 2 - Gate Keras)
    3. TokenScorer & EntryEngine (Modul 3)
    4. PositionManager (Modul 4)
    5. RiskController (Modul 5 - Non-overridable)
    6. TradeAuditLogger & TelegramNotifier (Modul 6)
    7. ParameterOptimizer & ConfigVersionManager (Modul 7)
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database | None = None,
        initial_capital_sol: float = 10.0,
    ) -> None:
        self.config = config
        self.db = database or Database(config=config.database)
        self.initial_capital_sol = initial_capital_sol

        self.notifier = TelegramNotifier(config=config)
        self.audit_logger = TradeAuditLogger(
            config=config,
            database=self.db,
            notifier=self.notifier,
        )
        self.executor = TransactionExecutor(config=config)
        self.position_manager = PositionManager(
            config=config,
            database=self.db,
            executor=self.executor,
        )
        self.risk_controller = RiskController(
            config=config,
            database=self.db,
            position_manager=self.position_manager,
            initial_capital_sol=initial_capital_sol,
        )
        self.screener = SecurityScreener(config=config, database=self.db)
        self.scorer = TokenScorer(config=config, database=self.db)
        self.entry_engine = EntryEngine(
            config=config,
            database=self.db,
            scorer=self.scorer,
            executor=self.executor,
        )
        self.detector = TokenDetector(
            config=config,
            database=self.db,
            on_candidate_found=self.process_candidate_token,
        )
        self.optimizer = ParameterOptimizer(config=config, database=self.db)
        self._last_prices: dict[str, float] = {}
        self._symbols: dict[str, str] = {}

    def get_scaled_trade_pnl_sol(self, trade_row: dict[str, Any]) -> float:
        """
        Return trade PnL in SOL scaled proportionally to `self.initial_capital_sol`
        if the historical trade was recorded under a much larger capital base (e.g. 10 SOL).
        """
        raw_pnl = float(trade_row.get("pnl_sol") or 0.0)
        entry_amt = float(trade_row.get("entry_amount_sol") or 0.0)
        target_entry_sol = self.initial_capital_sol * (
            self.config.position.max_position_pct / 100.0
        )
        if entry_amt > 0 and target_entry_sol > 0 and entry_amt > target_entry_sol * 1.5:
            scale = target_entry_sol / entry_amt
            return round(raw_pnl * scale, 6)
        return raw_pnl

    def set_capital_sync(self, new_capital_sol: float) -> dict[str, Any]:
        """
        Synchronously update the bot's starting capital (e.g. 0.5 SOL) and rescale
        realized equity & open positions so all future entries follow the new equity base.
        """
        if new_capital_sol <= 0:
            raise ValueError("Modal (capital) harus lebih besar dari 0 SOL.")
        prev_initial = self.initial_capital_sol if self.initial_capital_sol > 0 else float(new_capital_sol)
        prev_realized_pnl = self.risk_controller.current_capital_sol - prev_initial
        cap_ratio = float(new_capital_sol) / prev_initial if prev_initial > 0 else 1.0

        self.initial_capital_sol = round(float(new_capital_sol), 6)
        scaled_pnl_sol = round(prev_realized_pnl * cap_ratio, 6)
        new_realized_cap = max(0.001, round(self.initial_capital_sol + scaled_pnl_sol, 6))
        self.risk_controller.initial_capital_sol = self.initial_capital_sol
        self.risk_controller.current_capital_sol = new_realized_cap
        self.risk_controller.peak_capital_sol = max(self.initial_capital_sol, new_realized_cap)

        target_entry_sol = self.initial_capital_sol * (
            self.config.position.max_position_pct / 100.0
        )
        for state in self.position_manager.open_positions.values():
            if state.entry_amount_sol > 0 and target_entry_sol > 0 and state.entry_amount_sol != target_entry_sol:
                ratio = target_entry_sol / state.entry_amount_sol
                state.entry_amount_sol = round(state.entry_amount_sol * ratio, 6)
                state.initial_tokens = state.initial_tokens * ratio
                state.remaining_tokens = state.remaining_tokens * ratio
                state.entry_fee_sol = round(state.entry_fee_sol * ratio, 6)
                state.realized_sol = round(state.realized_sol * ratio, 6)
                state.realized_fee_sol = round(state.realized_fee_sol * ratio, 6)

        return self.get_live_equity_summary()

    async def set_capital(self, new_capital_sol: float) -> dict[str, Any]:
        """Async wrapper around set_capital_sync."""
        return self.set_capital_sync(new_capital_sol)

    async def initialize(self) -> None:
        """Connect database, restore any open positions, and record baseline config version."""
        await self.db.connect()
        await self.optimizer.version_manager.create_initial_version(
            self.optimizer.get_current_tunable_params()
        )
        # Apply active learned config version from DB into runtime BotConfig
        active_ver = await self.db.get_active_config_version()
        if active_ver and active_ver.get("config_snapshot"):
            try:
                import json
                raw_snap = active_ver["config_snapshot"]
                snap = json.loads(raw_snap) if isinstance(raw_snap, str) else raw_snap
                if isinstance(snap, dict):
                    for k, v in snap.items():
                        if isinstance(v, (int, float)):
                            self.optimizer._apply_param_to_config(k, float(v))
            except Exception as e:
                logger.warning("Failed to apply active config version from DB: %s", e)
        # Sync historical realized PnL from closed trades in DB (scaled to initial_capital_sol)
        closed_rows = await self.db.get_all_closed_trades()
        hist_pnl_sol = sum(self.get_scaled_trade_pnl_sol(r) for r in closed_rows)
        if abs(hist_pnl_sol) > 1e-9:
            self.risk_controller.current_capital_sol = max(
                0.001, round(self.initial_capital_sol + hist_pnl_sol, 6)
            )
            if self.risk_controller.current_capital_sol > self.risk_controller.peak_capital_sol:
                self.risk_controller.peak_capital_sol = self.risk_controller.current_capital_sol

        # Restore open positions from DB if any exist from previous session
        target_entry_sol = self.initial_capital_sol * (
            self.config.position.max_position_pct / 100.0
        )
        open_rows = await self.db.get_open_trades()
        for r in open_rows:
            if r["id"] not in self.position_manager.open_positions:
                raw_entry_sol = float(r["entry_amount_sol"] or 0.0)
                raw_tokens = float(r["entry_amount_tokens"] or 0.0)
                raw_fee = float(r["entry_fee_sol"] or 0.0)
                if raw_entry_sol > target_entry_sol * 1.5 and target_entry_sol > 0:
                    ratio = target_entry_sol / raw_entry_sol
                    raw_entry_sol = round(raw_entry_sol * ratio, 6)
                    raw_tokens = raw_tokens * ratio
                    raw_fee = round(raw_fee * ratio, 6)
                tr = TradeRecord(
                    id=r["id"],
                    token_id=r["token_id"],
                    scoring_id=r.get("scoring_id"),
                    mint_address=r["mint_address"],
                    mode=r["mode"],
                    entry_price_usd=r["entry_price_usd"],
                    entry_amount_sol=raw_entry_sol,
                    entry_amount_tokens=raw_tokens,
                    entry_slippage_pct=r["entry_slippage_pct"],
                    entry_fee_sol=raw_fee,
                    entry_at=r["entry_at"],
                )
                self.position_manager.register_trade(tr)
                if r["entry_price_usd"]:
                    self._last_prices[r["mint_address"]] = float(r["entry_price_usd"])
                tok_row = await self.db.get_token_by_mint(r["mint_address"])
                if tok_row and tok_row.get("symbol"):
                    self._symbols[r["mint_address"]] = tok_row["symbol"]

        logger.info(
            "MemeTradingBot initialized [mode=%s, capital=%.4f SOL, open_positions=%d]",
            self.config.mode.value,
            self.risk_controller.current_capital_sol,
            self.position_manager.open_count,
        )

        # Launch non-blocking Telegram command listener for instant /status, /start, /kill responses
        async def _bot_status() -> str:
            return f"<pre>{self.format_live_dashboard()}</pre>"

        self.notifier.start_background_listener(
            on_kill_command=lambda r: self.risk_controller.trigger_telegram_kill(r),
            on_status_command=_bot_status,
        )

    async def shutdown(self) -> None:
        """Gracefully close all network clients and database connections."""
        await self.detector.stop()
        await self.screener.close()
        await self.executor.close()
        await self.notifier.close()
        await self.db.close()
        logger.info("MemeTradingBot shut down cleanly.")

    async def process_candidate_token(
        self,
        token: TokenSnapshot,
        price_change_5m_pct: float | None = None,
        mock_screening_kwargs: dict[str, Any] | None = None,
    ) -> TradeRecord | None:
        """
        Full pipeline for a single candidate token:
        Risk Gate -> Security Screening (Gate Keras) -> Scoring -> Position Sizing -> Entry -> Register Position.
        """
        # 1. Pre-trade Risk Gate & Max Open Positions Check
        if self.position_manager.open_count >= self.config.position.max_open_positions:
            logger.info(
                "Candidate %s skipped: max_open_positions reached (%d/%d)",
                token.mint_address,
                self.position_manager.open_count,
                self.config.position.max_open_positions,
            )
            return None

        risk_check = await self.risk_controller.check_can_trade()
        if not risk_check.allowed:
            logger.warning(
                "Candidate %s blocked by RiskController: %s",
                token.mint_address,
                risk_check.reason,
            )
            if risk_check.event_type is not None:
                await self.audit_logger.log_risk_alert(
                    event_type=risk_check.event_type.value,
                    reason=risk_check.reason or "Risk limit triggered",
                    daily_pnl_sol=self.risk_controller.get_daily_pnl_sol(),
                    drawdown_pct=self.risk_controller.current_drawdown_pct,
                )
            return None

        # 2. Ensure token snapshot is stored in SQLite
        existing = await self.db.get_token_by_mint(token.mint_address)
        if existing:
            token_id = int(existing["id"])
        else:
            token_id = await self.db.insert_token(token)

        # 3. Modul 2: Security Screening (Gate Keras)
        screening_kwargs = mock_screening_kwargs or {}
        screening_res = await self.screener.screen_token(
            token=token,
            token_id=token_id,
            **screening_kwargs,
        )
        if not screening_res.passed:
            return None

        # 4. Modul 3: Scoring & Entry (dynamically follows Live Equity & Available Cash)
        eq = self.get_live_equity_summary()
        trade = await self.entry_engine.evaluate_and_enter(
            token=token,
            token_id=token_id,
            screening_result=screening_res,
            wallet_capital_sol=eq["live_equity_sol"],
            available_cash_sol=eq["cash_available_sol"],
            price_change_5m_pct=price_change_5m_pct,
        )

        if trade is None:
            return None

        # 5. Record successful tx in RiskController and register open position in PositionManager
        await self.risk_controller.record_transaction_result(success=True)
        self._last_prices[token.mint_address] = trade.entry_price_usd or token.initial_price_usd or 0.0
        self._symbols[token.mint_address] = token.symbol or token.mint_address[:6]
        self.position_manager.register_trade(
            trade=trade,
            initial_liquidity_usd=token.initial_liquidity_usd,
        )
        await self.audit_logger.log_entry(
            trade=trade,
            screening=screening_res,
            symbol=token.symbol,
        )
        print(
            f"\n🟢 [BUY EXECUTED - {self.config.mode.value}] "
            f"Token: {token.symbol or token.mint_address[:8]} ({token.mint_address}) | "
            f"Waktu: {trade.entry_at} | "
            f"Size: {trade.entry_amount_sol:.4f} SOL -> {trade.entry_amount_tokens:,.2f} token @ ${trade.entry_price_usd:.8f} | "
            f"Score: {trade.entry_score:.4f}",
            flush=True,
        )
        return trade

    async def monitor_open_positions(
        self,
        price_by_mint: dict[str, float],
        liquidity_by_mint: dict[str, float] | None = None,
        rug_indicators_by_mint: dict[str, list[str]] | None = None,
        now_timestamp: float | None = None,
    ) -> list[int]:
        """
        Evaluate exit conditions for all open positions and update RiskController
        whenever a position closes completely.
        Returns list of trade_ids that closed on this tick.
        """
        liquidity_by_mint = liquidity_by_mint or {}
        rug_indicators_by_mint = rug_indicators_by_mint or {}
        closed_ids: list[int] = []

        for trade_id in list(self.position_manager.open_positions.keys()):
            state = self.position_manager.open_positions.get(trade_id)
            if state is None:
                continue

            mint = state.mint_address
            cur_price = price_by_mint.get(mint, self._last_prices.get(mint, state.entry_price_usd))
            self._last_prices[mint] = cur_price
            cur_liq = liquidity_by_mint.get(mint, state.initial_liquidity_usd)
            rug_inds = rug_indicators_by_mint.get(mint)

            sig = await self.position_manager.evaluate_and_manage(
                trade_id=trade_id,
                current_price_usd=cur_price,
                current_liquidity_usd=cur_liq,
                active_rug_indicators=rug_inds,
                now_timestamp=now_timestamp,
            )

            # Check if position was closed on this tick
            if trade_id not in self.position_manager.open_positions:
                closed_ids.append(trade_id)
                cursor = await self.db.conn.execute(
                    "SELECT * FROM trades WHERE id = ?", (trade_id,)
                )
                row = await cursor.fetchone()
                if row is not None:
                    pnl_sol = float(row["pnl_sol"] or 0.0)
                    pnl_pct = float(row["pnl_pct"] or 0.0)
                    sym = self._symbols.get(mint, mint[:8])
                    icon = "💰" if pnl_sol >= 0 else "🔻"
                    print(
                        f"\n{icon} [POSITION CLOSED] Trade #{trade_id} {sym} ({mint[:8]}...) | "
                        f"Reason: {row['exit_reason']} | Exit: ${float(row['exit_price_usd'] or 0.0):.8f} | "
                        f"PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.2f}%)",
                        flush=True,
                    )
                    risk_res = await self.risk_controller.record_trade_closed(
                        pnl_sol=pnl_sol,
                        now_timestamp=now_timestamp,
                    )
                    if not risk_res.allowed and risk_res.event_type is not None:
                        await self.audit_logger.log_risk_alert(
                            event_type=risk_res.event_type.value,
                            reason=risk_res.reason or "Risk limit breached",
                            daily_pnl_sol=self.risk_controller.get_daily_pnl_sol(),
                            drawdown_pct=self.risk_controller.current_drawdown_pct,
                        )
            elif sig.should_exit and sig.reason is not None:
                sym = self._symbols.get(mint, mint[:8])
                print(
                    f"\n💰 [PARTIAL SELL] Trade #{trade_id} {sym} ({mint[:8]}...) | "
                    f"Reason: {sig.reason.value} | Sold: {sig.sell_pct:.0f}% @ ${cur_price:.8f}",
                    flush=True,
                )

        return closed_ids

    async def refresh_open_positions(self) -> list[int]:
        """
        Fetch latest real-time prices and liquidity from DexScreener for all open positions
        and evaluate TP / SL / Trailing / Time-Stop / Emergency Exit rules.
        """
        if self.position_manager.open_count == 0:
            return []

        price_map: dict[str, float] = {}
        liq_map: dict[str, float] = {}

        open_mints = [
            state.mint_address for state in self.position_manager.open_positions.values()
        ]
        pairs = await self.detector.dexscreener.get_tokens_pairs_batch(open_mints)
        for p in pairs:
            snap = self.detector.dexscreener.parse_pair_to_snapshot(p)
            if snap and snap.mint_address not in price_map:
                if snap.initial_price_usd:
                    price_map[snap.mint_address] = snap.initial_price_usd
                if snap.initial_liquidity_usd is not None:
                    liq_map[snap.mint_address] = snap.initial_liquidity_usd

        return await self.monitor_open_positions(
            price_by_mint=price_map,
            liquidity_by_mint=liq_map,
        )

    def get_live_equity_summary(self) -> dict[str, Any]:
        """
        Compute real-time equity, realized capital, unrealized PnL, and per-position status.
        """
        sol_usd = self.config.execution.paper_sol_price_usd
        realized_cap_sol = self.risk_controller.current_capital_sol
        unrealized_pnl_sol = 0.0
        locked_entry_sol = 0.0
        positions_detail: list[dict[str, Any]] = []

        for trade_id, state in self.position_manager.open_positions.items():
            cur_px = self._last_prices.get(state.mint_address, state.entry_price_usd)
            rem_value_usd = state.remaining_tokens * cur_px
            rem_value_sol = rem_value_usd / sol_usd if sol_usd > 0 else 0.0
            pos_net_pnl_sol = (
                (state.realized_sol + rem_value_sol)
                - state.entry_amount_sol
                - state.entry_fee_sol
                - state.realized_fee_sol
            )
            pos_pnl_pct = (
                ((cur_px - state.entry_price_usd) / state.entry_price_usd) * 100.0
                if state.entry_price_usd > 0
                else 0.0
            )
            unrealized_pnl_sol += pos_net_pnl_sol
            locked_entry_sol += state.entry_amount_sol
            positions_detail.append(
                {
                    "trade_id": trade_id,
                    "symbol": self._symbols.get(state.mint_address, state.mint_address[:8]),
                    "mint_address": state.mint_address,
                    "entry_price_usd": state.entry_price_usd,
                    "current_price_usd": cur_px,
                    "entry_sol": state.entry_amount_sol,
                    "remaining_tokens": state.remaining_tokens,
                    "pnl_sol": round(pos_net_pnl_sol, 6),
                    "pnl_pct": round(pos_pnl_pct, 2),
                    "tp_hits": len(state.triggered_tp_indices),
                }
            )

        live_equity_sol = round(realized_cap_sol + unrealized_pnl_sol, 6)
        cash_available_sol = round(max(0.0, realized_cap_sol - locked_entry_sol), 6)

        return {
            "initial_capital_sol": self.initial_capital_sol,
            "realized_capital_sol": round(realized_cap_sol, 6),
            "cash_available_sol": cash_available_sol,
            "locked_sol": round(locked_entry_sol, 6),
            "unrealized_pnl_sol": round(unrealized_pnl_sol, 6),
            "live_equity_sol": live_equity_sol,
            "live_equity_usd": round(live_equity_sol * sol_usd, 2),
            "daily_pnl_sol": self.risk_controller.get_daily_pnl_sol(),
            "drawdown_pct": self.risk_controller.current_drawdown_pct,
            "open_positions": positions_detail,
        }

    def format_live_dashboard(self, cycle_num: int, discovered_count: int = 0) -> str:
        """Render a concise live equity & open positions status block for terminal display."""
        eq = self.get_live_equity_summary()
        total_pnl_sol = eq["live_equity_sol"] - eq["initial_capital_sol"]
        total_pnl_pct = (
            (total_pnl_sol / eq["initial_capital_sol"]) * 100.0
            if eq["initial_capital_sol"] > 0
            else 0.0
        )
        lines = [
            "-" * 78,
            (
                f"[CYCLE #{cycle_num}] Mode: {self.config.mode.value} | "
                f"Live Equity: {eq['live_equity_sol']:.4f} SOL (${eq['live_equity_usd']:,.2f}) "
                f"[{total_pnl_sol:+.4f} SOL / {total_pnl_pct:+.2f}%]"
            ),
            (
                f"  Cash Available: {eq['cash_available_sol']:.4f} SOL | "
                f"Unrealized PnL: {eq['unrealized_pnl_sol']:+.4f} SOL | "
                f"Drawdown: {eq['drawdown_pct']:.2f}% | "
                f"New Candidates: {discovered_count} | "
                f"Open Positions: {len(eq['open_positions'])}/{self.config.position.max_open_positions}"
            ),
        ]
        if eq["open_positions"]:
            lines.append("  POSISI TERBUKA (LIVE):")
            for p in eq["open_positions"]:
                lines.append(
                    f"    * [#{p['trade_id']}] {p['symbol']} ({p['mint_address'][:8]}...) | "
                    f"Buy: ${p['entry_price_usd']:.8f} -> Now: ${p['current_price_usd']:.8f} | "
                    f"PnL: {p['pnl_sol']:+.4f} SOL ({p['pnl_pct']:+.2f}%) | TP Hit: {p['tp_hits']}"
                )
        lines.append("-" * 78)
        return "\n".join(lines)

    async def run_learning_cycle(self, period_label: str = "daily") -> dict[str, Any]:
        """
        Execute Modul 7 periodic post-trade analysis, check for version degradation
        (auto-rollback), and generate a validated optimization proposal if eligible.
        """
        closed_trades = await self.db.get_all_closed_trades()
        rolled_back, restored_params, rb_reason = (
            await self.optimizer.version_manager.check_and_rollback_if_degraded(
                recent_trades=closed_trades,
                initial_capital_sol=self.initial_capital_sol,
            )
        )

        report, proposal, validation = await self.optimizer.generate_proposal_from_trades(
            trades=closed_trades,
            period_label=period_label,
        )

        return {
            "rolled_back": rolled_back,
            "restored_params": restored_params,
            "rollback_reason": rb_reason,
            "report": report,
            "proposal": proposal,
            "validation": validation,
        }


def verify_live_mode_confirmation(config: BotConfig, interactive_input: str | None = None) -> None:
    """
    Require explicit flag AND exact confirmation phrase before allowing LIVE mode startup.
    """
    if config.mode != BotMode.LIVE:
        return
    if config.live_confirm_required:
        raise LiveModeError(
            "LIVE mode blocked: `bot.live_confirm_required` is still true in config.yaml."
        )
    phrase = interactive_input if interactive_input is not None else input(
        f"PERINGATAN: Anda akan menjalankan mode LIVE dengan dana nyata.\n"
        f"Ketik persis '{LIVE_CONFIRMATION_PHRASE}' untuk konfirmasi: "
    )
    if phrase.strip() != LIVE_CONFIRMATION_PHRASE:
        raise LiveModeError("Konfirmasi manual LIVE ditolak (frasa tidak cocok). Bot dibatalkan.")


async def _main_async() -> None:
    parser = argparse.ArgumentParser(description="Solana Meme Coin Trading Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--capital", type=float, default=10.0, help="Initial bot capital in SOL")
    parser.add_argument("--cycles", type=int, default=1, help="Number of polling cycles to run (0=infinite)")
    parser.add_argument(
        "--learn-every",
        type=int,
        default=10,
        help="Run Modul 7 learning cycle every N polling cycles (default: 10)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Web UI bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Web UI port (default: 8080)")
    parser.add_argument("--no-ui", action="store_true", help="Disable Web UI server")
    args = parser.parse_args()

    config = load_config(config_path=args.config)
    setup_logging(config)

    if config.mode == BotMode.LIVE:
        verify_live_mode_confirmation(config)

    bot = MemeTradingBot(config=config, initial_capital_sol=args.capital)
    await bot.initialize()

    ui_server = None
    ui_controller = None
    if not args.no_ui:
        try:
            from src.ui.server import BotUIController, start_background_ui_server

            ui_controller = BotUIController(
                bot=bot,
                loop=asyncio.get_running_loop(),
                config_path=args.config,
                is_running=True,
            )
            ui_server = start_background_ui_server(ui_controller, host=args.host, port=args.port)
            print(
                f"\n🌐 Web UI Dashboard aktif di: http://127.0.0.1:{args.port} (Host bind: {args.host}:{args.port})\n",
                flush=True,
            )
        except OSError as e:
            logger.warning("Could not bind Web UI on port %d (%s); continuing without UI", args.port, e)

    try:
        logger.info("Bot running in %s mode (cycles=%d)", config.mode.value, args.cycles)
        count = 0
        while args.cycles == 0 or count < args.cycles:
            # Always listen and respond to Telegram commands even if Auto-Scan is paused!
            async def _get_status_text() -> str:
                return f"<pre>{bot.format_live_dashboard(cycle_num=count)}</pre>"

            await bot.notifier.poll_commands(
                on_kill_command=lambda r: bot.risk_controller.trigger_telegram_kill(r),
                on_status_command=_get_status_text,
            )

            if ui_controller is not None and not ui_controller.is_running:
                await asyncio.sleep(1.0)
                continue

            count += 1
            if ui_controller is not None:
                ui_controller.cycle_count = count

            # Check critical risk boundaries (daily loss, drawdown, circuit breaker, kill switch)
            # Note: Max open positions should NOT kill the main loop; the bot must keep monitoring & managing existing open positions!
            risk_ok = await bot.risk_controller.check_can_trade()
            if not risk_ok.allowed and risk_ok.event_type != RiskEventType.MAX_POSITIONS:
                logger.warning("Stopping main loop: %s", risk_ok.reason)
                break

            # 1. Detect & process new candidate tokens only if we have free position slots
            discovered = []
            if bot.position_manager.open_count < bot.config.position.max_open_positions and risk_ok.allowed:
                discovered = await bot.detector.poll_new_tokens()

            # 2. Refresh prices & manage open positions (Modul 4 -> 5)
            await bot.refresh_open_positions()

            # 3. Print Live Equity & Open Positions Dashboard to terminal
            print(bot.format_live_dashboard(cycle_num=count, discovered_count=len(discovered)), flush=True)

            # 4. Periodic self-learning cycle (Modul 7)
            if args.learn_every > 0 and count % args.learn_every == 0:
                learn_res = await bot.run_learning_cycle(period_label=f"cycle_{count}")
                if learn_res["rolled_back"]:
                    print(
                        f"\n🔄 [AUTO-ROLLBACK] {learn_res['rollback_reason']} | "
                        f"Restored: {learn_res['restored_params']}",
                        flush=True,
                    )
                if learn_res["proposal"] is not None:
                    prop = learn_res["proposal"]
                    logger.info(
                        "Learning cycle produced proposal: %s (validated=%s, status=%s)",
                        prop.proposed_changes,
                        prop.passed_validation,
                        prop.status.value,
                    )
                    if prop.status.value == "auto_applied":
                        print(
                            f"\n🧠 [AUTO-LEARN APPLIED] Version {prop.applied_version} | "
                            f"{prop.proposed_changes['parameter']}: "
                            f"{prop.proposed_changes['old_value']} -> {prop.proposed_changes['new_value']}",
                            flush=True,
                        )

            if args.cycles == 0 or count < args.cycles:
                await asyncio.sleep(config.detection.poll_interval_sec)
    finally:
        if ui_server is not None:
            ui_server.shutdown()
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(_main_async())

