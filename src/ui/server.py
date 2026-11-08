"""Web UI & REST API server for Solana Meme Coin Trading Bot.

Can run either:
1. Embedded inside `python -m src.bot` (sharing the active MemeTradingBot instance and event loop), or
2. Standalone via `python -m src.ui.server --port 8080` (managing its own MemeTradingBot instance).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import os
import yaml

from src.bot import LIVE_CONFIRMATION_PHRASE, MemeTradingBot
from src.core.config import BotConfig, TakeProfitLevel, load_config
from src.core.enums import BotMode, ExitReason, RiskEventType
from src.learning.backtester import compute_performance_metrics
from src.position.exit_rules import ExitSignal

logger = logging.getLogger(__name__)

DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"


class BotUIController:
    """Thread-safe bridge between the HTTP UI server and the async MemeTradingBot."""

    def __init__(
        self,
        bot: MemeTradingBot,
        loop: asyncio.AbstractEventLoop,
        config_path: str | Path = "config.yaml",
        is_running: bool = False,
    ) -> None:
        self.bot = bot
        self.loop = loop
        self.config_path = Path(config_path)
        self.is_running = is_running
        self.cycle_count = 0
        self._bg_scan_task: asyncio.Future | None = None

        # Bind live UI dashboard callback to Telegram listener
        async def _ui_status() -> str:
            return f"<pre>{self.bot.format_live_dashboard(cycle_num=self.cycle_count)}</pre>"

        self.bot.notifier.start_background_listener(
            on_kill_command=lambda r: self.bot.risk_controller.trigger_telegram_kill(r),
            on_status_command=_ui_status,
            loop=self.loop,
        )

    def run_coro(self, coro: Any, timeout: float = 25.0) -> Any:
        """Schedule a coroutine on the bot's event loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def _update_yaml_key(self, pattern: str, replacement: str) -> None:
        """Update a specific key in config.yaml while preserving comments."""
        if not self.config_path.exists():
            return
        try:
            text = self.config_path.read_text(encoding="utf-8")
            new_text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
            self.config_path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            logger.warning("Could not update %s: %s", self.config_path, e)

    async def _background_scan_loop(self) -> None:
        """Continuous polling loop when started from the UI."""
        logger.info("UI Auto-Scan loop started.")
        while self.is_running:
            try:
                risk_ok = await self.bot.risk_controller.check_can_trade()
                if not risk_ok.allowed and risk_ok.event_type != RiskEventType.MAX_POSITIONS:
                    logger.warning("Auto-scan paused by risk controller: %s", risk_ok.reason)
                    self.is_running = False
                    break

                self.cycle_count += 1
                discovered = []
                if self.bot.position_manager.open_count < self.bot.config.position.max_open_positions and risk_ok.allowed:
                    discovered = await self.bot.detector.poll_new_tokens()
                await self.bot.refresh_open_positions()
                print(
                    self.bot.format_live_dashboard(
                        cycle_num=self.cycle_count, discovered_count=len(discovered)
                    ),
                    flush=True,
                )

                if self.cycle_count % 10 == 0:
                    await self.bot.run_learning_cycle(period_label=f"ui_cycle_{self.cycle_count}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in UI auto-scan loop: %s", e)

            await asyncio.sleep(self.bot.config.detection.poll_interval_sec)
        logger.info("UI Auto-Scan loop stopped.")

    def toggle_engine(self) -> dict[str, Any]:
        """Start or pause the background auto-scan loop."""
        if self.is_running:
            self.is_running = False
            if self._bg_scan_task and not self._bg_scan_task.done():
                self._bg_scan_task.cancel()
            return {"is_running": False, "message": "Auto-Scan dihentikan sementara (Paused)."}
        else:
            self.is_running = True
            self._bg_scan_task = asyncio.run_coroutine_threadsafe(
                self._background_scan_loop(), self.loop
            )
            return {"is_running": True, "message": "Auto-Scan diaktifkan! Bot mulai memindai token baru."}

    def scan_once(self) -> dict[str, Any]:
        """Run a single detection + position refresh cycle immediately."""
        async def _do_scan() -> int:
            self.cycle_count += 1
            discovered = await self.bot.detector.poll_new_tokens()
            await self.bot.refresh_open_positions()
            print(
                self.bot.format_live_dashboard(
                    cycle_num=self.cycle_count, discovered_count=len(discovered)
                ),
                flush=True,
            )
            return len(discovered)

        count = self.run_coro(_do_scan(), timeout=30.0)
        return {
            "discovered": count,
            "message": f"Siklus #{self.cycle_count} selesai: {count} kandidat token baru dievaluasi.",
        }

    def switch_mode(self, target_mode: str, confirmation_phrase: str = "") -> dict[str, Any]:
        """Switch between PAPER and LIVE modes with strict safety verification."""
        mode_upper = target_mode.strip().upper()
        if mode_upper not in ("PAPER", "LIVE"):
            raise ValueError(f"Mode tidak valid: {target_mode}")

        if mode_upper == "LIVE":
            if confirmation_phrase.strip() != LIVE_CONFIRMATION_PHRASE:
                raise ValueError(
                    f"Frasa konfirmasi salah. Ketik persis: '{LIVE_CONFIRMATION_PHRASE}'"
                )
            object.__setattr__(self.bot.config, "mode", BotMode.LIVE)
            object.__setattr__(self.bot.config, "live_confirm_required", False)
            self._update_yaml_key(r"^(\s*mode:\s*)(?:PAPER|LIVE)", r"\g<1>LIVE")
            self._update_yaml_key(
                r"^(\s*live_confirm_required:\s*)(?:true|false)", r"\g<1>false"
            )
            logger.warning("Bot switched to LIVE mode via Web UI!")
            return {
                "mode": "LIVE",
                "message": "Mode LIVE (Dana Nyata) telah diaktifkan! Semua aturan batas risiko tetap aktif.",
            }
        else:
            object.__setattr__(self.bot.config, "mode", BotMode.PAPER)
            object.__setattr__(self.bot.config, "live_confirm_required", True)
            self._update_yaml_key(r"^(\s*mode:\s*)(?:PAPER|LIVE)", r"\g<1>PAPER")
            self._update_yaml_key(
                r"^(\s*live_confirm_required:\s*)(?:true|false)", r"\g<1>true"
            )
            logger.info("Bot switched to PAPER mode via Web UI.")
            return {
                "mode": "PAPER",
                "message": "Berhasil kembali ke Mode PAPER (Simulasi Aman).",
            }

    def toggle_kill_switch(self, action: str) -> dict[str, Any]:
        """Activate or deactivate the emergency kill switch."""
        if action == "activate":
            async def _do_kill() -> None:
                self.bot.risk_controller.kill_switch.activate_file_flag("Activated via Web UI")
                await self.bot.risk_controller.check_can_trade()
                await self.bot.position_manager.close_all_positions(
                    reason=ExitReason.KILL_SWITCH,
                    price_lookup=self.bot._last_prices,
                )

            self.is_running = False
            self.run_coro(_do_kill())
            return {
                "kill_switch_active": True,
                "message": "🛑 KILL SWITCH DIAKTIFKAN! Semua posisi terbuka ditutup & bot dihentikan.",
            }
        else:
            self.bot.risk_controller.kill_switch.clear_all()
            self.bot.risk_controller.is_halted = False
            self.bot.risk_controller.halt_reason = None
            return {
                "kill_switch_active": False,
                "message": "🔓 Kill Switch dinonaktifkan. Bot siap dijalankan kembali.",
            }

    def move_stop_to_entry(self, trade_id: int) -> dict[str, Any]:
        """Move stop-loss to entry price (breakeven stop) for an open position."""
        pos = self.bot.position_manager.open_positions.get(int(trade_id))
        if not pos:
            raise ValueError(f"Posisi #{trade_id} tidak ditemukan atau sudah ditutup.")
        pos.stop_at_entry = True
        sym = self.bot._symbols.get(pos.mint_address, pos.mint_address[:6])
        return {
            "trade_id": int(trade_id),
            "stop_at_entry": True,
            "message": f"🛡️ {sym} stop moved to entry",
        }

    def set_capital(self, capital_sol: float) -> dict[str, Any]:
        """Update the bot's starting capital (SOL) and rescale live equity & entry sizing."""
        val = float(capital_sol)
        if val <= 0:
            raise ValueError("Modal harus lebih besar dari 0 SOL (contoh: 0.5).")
        eq = self.bot.set_capital_sync(val)
        next_entry = min(
            round((self.bot.config.position.max_position_pct / 100.0) * eq["live_equity_sol"], 6),
            self.bot.config.position.max_position_sol,
            eq["cash_available_sol"],
        )
        return {
            "initial_capital_sol": eq["initial_capital_sol"],
            "live_equity_sol": eq["live_equity_sol"],
            "next_entry_sol": round(next_entry, 4),
            "message": (
                f"💰 Modal disesuaikan ke {val:.4f} SOL | "
                f"Live Equity: {eq['live_equity_sol']:.4f} SOL | "
                f"Ukuran Entry Berikutnya ({self.bot.config.position.max_position_pct:.1f}%): {next_entry:.4f} SOL"
            ),
        }

    def close_position(self, trade_id_or_all: int | str, sell_pct: float = 100.0) -> dict[str, Any]:
        """Manually close (full or partial) one or all open positions at current market price."""
        pct_clamped = max(1.0, min(100.0, float(sell_pct or 100.0)))

        async def _do_close() -> int:
            closed_cnt = 0
            target_ids = (
                list(self.bot.position_manager.open_positions.keys())
                if str(trade_id_or_all).lower() == "all"
                else [int(trade_id_or_all)]
            )
            for tid in target_ids:
                pos = self.bot.position_manager.open_positions.get(tid)
                if not pos:
                    continue
                price = self.bot._last_prices.get(pos.mint_address, pos.entry_price_usd)
                sig = ExitSignal(
                    should_exit=True,
                    sell_pct=pct_clamped,
                    reason=ExitReason.KILL_SWITCH,
                    description=f"Manual sell ({pct_clamped:.0f}%) from Trading Desk UI",
                )
                ok = await self.bot.position_manager._execute_exit(
                    state=pos,
                    signal=sig,
                    current_price_usd=price,
                )
                if ok:
                    if pct_clamped >= 99.9:
                        row = await self.bot.db.get_trade_by_id(tid)
                        if row and row.get("pnl_sol") is not None:
                            await self.bot.risk_controller.record_trade_closed(
                                pnl_sol=float(row["pnl_sol"])
                            )
                    closed_cnt += 1
            return closed_cnt

        cnt = self.run_coro(_do_close())
        return {
            "closed_count": cnt,
            "message": f"⚡ Sold {pct_clamped:.0f}% on {cnt} position(s) at market price.",
        }

    def toggle_auto_learn(self, enabled: bool) -> dict[str, Any]:
        """Toggle auto_apply_in_paper in runtime config and config.yaml."""
        object.__setattr__(self.bot.config.learning, "auto_apply_in_paper", bool(enabled))
        object.__setattr__(self.bot.config.learning, "require_manual_approval", not bool(enabled))
        self._update_yaml_key(
            r"^(\s*auto_apply_in_paper:\s*)(?:true|false)",
            f"\\g<1>{'true' if enabled else 'false'}",
        )
        self._update_yaml_key(
            r"^(\s*require_manual_approval:\s*)(?:true|false)",
            f"\\g<1>{'false' if enabled else 'true'}",
        )
        return {
            "auto_apply_in_paper": bool(enabled),
            "message": (
                "Auto-Apply Learning (Paper) DIAKTIFKAN: parameter tervalidasi akan diterapkan otomatis."
                if enabled
                else "Auto-Apply Learning DINONAKTIFKAN: perubahan parameter menunggu persetujuan manual."
            ),
        }

    def run_learning_now(self) -> dict[str, Any]:
        """Run Modul 7 post-trade analysis and walk-forward optimization immediately."""
        res = self.run_coro(self.bot.run_learning_cycle(period_label="manual_ui"))
        prop = res.get("proposal")
        if prop is None:
            return {
                "message": "Analisa selesai: parameter saat ini sudah optimal (tidak ada usulan perubahan baru)."
            }
        chg = prop.proposed_changes
        return {
            "message": (
                f"Usulan dibuat: {chg['parameter']} ({chg['old_value']} -> {chg['new_value']}) | "
                f"Status: {prop.status.value}"
            )
        }

    def approve_proposal(self, proposal_id: int) -> dict[str, Any]:
        """Approve a pending optimization proposal by ID."""
        new_tag = self.run_coro(self.bot.optimizer.approve_proposal(int(proposal_id)))
        return {
            "version_tag": new_tag,
            "message": f"Proposal #{proposal_id} disetujui! Versi parameter aktif sekarang: {new_tag}",
        }

    def get_all_settings(self) -> dict[str, Any]:
        """Return all non-secret settings + masked status of .env secrets for Web UI editing."""
        cfg = self.bot.config
        tps = cfg.position.take_profit_levels
        return {
            "capital": {
                "initial_capital_sol": self.bot.initial_capital_sol,
            },
            "detection": {
                "poll_interval_sec": cfg.detection.poll_interval_sec,
                "min_liquidity_usd": cfg.detection.min_liquidity_usd,
                "max_token_age_sec": cfg.detection.max_token_age_sec,
                "min_volume_usd": cfg.detection.min_volume_usd,
                "dexscreener_enabled": cfg.detection.dexscreener_enabled,
                "birdeye_enabled": cfg.detection.birdeye_enabled,
                "helius_ws_enabled": cfg.detection.helius_ws_enabled,
            },
            "screening": {
                "min_liquidity_usd": cfg.screening.min_liquidity_usd,
                "top10_holder_max_pct": cfg.screening.top10_holder_max_pct,
                "lp_must_be_burned_or_locked": cfg.screening.lp_must_be_burned_or_locked,
                "max_sell_tax_pct": cfg.screening.max_sell_tax_pct,
                "max_transfer_fee_pct": cfg.screening.max_transfer_fee_pct,
                "rugcheck_enabled": cfg.screening.rugcheck_enabled,
                "rugcheck_min_score": cfg.screening.rugcheck_min_score,
                "deployer_check_enabled": cfg.screening.deployer_check_enabled,
                "deployer_max_rugged": cfg.screening.deployer_max_rugged,
                "pumpfun_enabled": cfg.screening.pumpfun_enabled,
                "pumpfun_top10_max_pct": cfg.screening.pumpfun_top10_max_pct,
                "pumpfun_min_curve_mcap_usd": cfg.screening.pumpfun_min_curve_mcap_usd,
                "pumpfun_min_age_sec": cfg.screening.pumpfun_min_age_sec,
                "pumpfun_block_bundle_risks": cfg.screening.pumpfun_block_bundle_risks,
                "pumpfun_max_m5_dump_pct": cfg.screening.pumpfun_max_m5_dump_pct,
            },
            "scoring": {
                "threshold": cfg.scoring.threshold,
                "w_price_momentum": cfg.scoring.weights.get("price_momentum", 0.25),
                "w_volume_liquidity_ratio": cfg.scoring.weights.get("volume_liquidity_ratio", 0.20),
                "w_holder_growth": cfg.scoring.weights.get("holder_growth", 0.20),
                "w_buy_sell_ratio": cfg.scoring.weights.get("buy_sell_ratio", 0.20),
                "w_token_age": cfg.scoring.weights.get("token_age", 0.15),
            },
            "position": {
                "max_position_pct": cfg.position.max_position_pct,
                "max_position_sol": cfg.position.max_position_sol,
                "max_open_positions": cfg.position.max_open_positions,
                "stop_loss_pct": cfg.position.stop_loss_pct,
                "trail_activation_pct": cfg.position.trailing_stop.activation_pct,
                "trail_pct": cfg.position.trailing_stop.trail_pct,
                "time_stop_minutes": cfg.position.time_stop_minutes,
                "emergency_liq_drop_pct": cfg.position.emergency.liquidity_drop_pct,
                "tp1_pct": tps[0].pct if len(tps) > 0 else 50.0,
                "tp1_sell_pct": tps[0].sell_pct if len(tps) > 0 else 30.0,
                "tp2_pct": tps[1].pct if len(tps) > 1 else 100.0,
                "tp2_sell_pct": tps[1].sell_pct if len(tps) > 1 else 30.0,
                "tp3_pct": tps[2].pct if len(tps) > 2 else 200.0,
                "tp3_sell_pct": tps[2].sell_pct if len(tps) > 2 else 100.0,
            },
            "execution": {
                "max_slippage_bps": cfg.execution.max_slippage_bps,
                "priority_fee_strategy": cfg.execution.priority_fee_strategy,
                "priority_fee_fixed_lamports": cfg.execution.priority_fee_fixed_lamports,
                "use_jito_bundle": cfg.execution.use_jito_bundle,
                "jito_tip_lamports": cfg.execution.jito_tip_lamports,
                "paper_sol_price_usd": cfg.execution.paper_sol_price_usd,
            },
            "risk": {
                "daily_loss_limit_sol": cfg.risk.daily_loss_limit_sol,
                "max_drawdown_pct": cfg.risk.max_drawdown_pct,
                "consecutive_failures": cfg.risk.circuit_breaker.consecutive_failures,
                "pause_duration_sec": cfg.risk.circuit_breaker.pause_duration_sec,
            },
            "learning": {
                "auto_apply_in_paper": cfg.learning.auto_apply_in_paper,
                "min_trades_for_proposal": cfg.learning.min_trades_for_proposal,
                "max_param_change_pct": cfg.learning.max_param_change_pct,
            },
            "server_env": {
                "solana_rpc_url": cfg.secrets.solana_rpc_url,
                "helius_ws_url": cfg.secrets.helius_ws_url,
                "helius_api_key_set": bool(cfg.secrets.helius_api_key),
                "birdeye_api_key_set": bool(cfg.secrets.birdeye_api_key),
                "wallet_private_key_set": bool(cfg.secrets.wallet_private_key),
                "telegram_enabled": cfg.telegram.enabled,
                "telegram_bot_token_set": bool(cfg.secrets.telegram_bot_token),
                "telegram_chat_id": cfg.secrets.telegram_chat_id,
            },
        }

    def _update_env_file(self, env_updates: dict[str, str]) -> None:
        """Persist non-empty secret/env updates to `.env` in workspace root."""
        env_path = self.config_path.resolve().parent / ".env"
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        updated_keys: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in env_updates:
                new_lines.append(f"{k}={env_updates[k]}")
                updated_keys.add(k)
            else:
                new_lines.append(line)
        for k, v in env_updates.items():
            if k not in updated_keys:
                new_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def update_all_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Apply comprehensive settings from Web UI into runtime BotConfig, config.yaml, and .env.
        """
        cfg = self.bot.config

        # 1. Capital
        cap_sec = payload.get("capital") or {}
        if "initial_capital_sol" in cap_sec and float(cap_sec["initial_capital_sol"] or 0) > 0:
            self.bot.set_capital_sync(float(cap_sec["initial_capital_sol"]))

        # 2. Detection
        det = payload.get("detection") or {}
        if det:
            if "poll_interval_sec" in det:
                object.__setattr__(cfg.detection, "poll_interval_sec", max(2, int(det["poll_interval_sec"])))
            if "min_liquidity_usd" in det:
                object.__setattr__(cfg.detection, "min_liquidity_usd", max(0.0, float(det["min_liquidity_usd"])))
            if "max_token_age_sec" in det:
                object.__setattr__(cfg.detection, "max_token_age_sec", max(60, int(det["max_token_age_sec"])))
            if "min_volume_usd" in det:
                object.__setattr__(cfg.detection, "min_volume_usd", max(0.0, float(det["min_volume_usd"])))
            if "dexscreener_enabled" in det:
                object.__setattr__(cfg.detection, "dexscreener_enabled", bool(det["dexscreener_enabled"]))
            if "birdeye_enabled" in det:
                object.__setattr__(cfg.detection, "birdeye_enabled", bool(det["birdeye_enabled"]))
            if "helius_ws_enabled" in det:
                object.__setattr__(cfg.detection, "helius_ws_enabled", bool(det["helius_ws_enabled"]))

        # 3. Screening + Pump.fun Gate
        scr = payload.get("screening") or {}
        if scr:
            if "min_liquidity_usd" in scr:
                object.__setattr__(cfg.screening, "min_liquidity_usd", max(0.0, float(scr["min_liquidity_usd"])))
            if "top10_holder_max_pct" in scr:
                object.__setattr__(cfg.screening, "top10_holder_max_pct", float(scr["top10_holder_max_pct"]))
            if "lp_must_be_burned_or_locked" in scr:
                object.__setattr__(cfg.screening, "lp_must_be_burned_or_locked", bool(scr["lp_must_be_burned_or_locked"]))
            if "max_sell_tax_pct" in scr:
                object.__setattr__(cfg.screening, "max_sell_tax_pct", float(scr["max_sell_tax_pct"]))
            if "max_transfer_fee_pct" in scr:
                object.__setattr__(cfg.screening, "max_transfer_fee_pct", float(scr["max_transfer_fee_pct"]))
            if "rugcheck_enabled" in scr:
                object.__setattr__(cfg.screening, "rugcheck_enabled", bool(scr["rugcheck_enabled"]))
            if "rugcheck_min_score" in scr:
                object.__setattr__(cfg.screening, "rugcheck_min_score", float(scr["rugcheck_min_score"]))
            if "deployer_check_enabled" in scr:
                object.__setattr__(cfg.screening, "deployer_check_enabled", bool(scr["deployer_check_enabled"]))
            if "deployer_max_rugged" in scr:
                object.__setattr__(cfg.screening, "deployer_max_rugged", int(scr["deployer_max_rugged"]))
            if "pumpfun_enabled" in scr:
                object.__setattr__(cfg.screening, "pumpfun_enabled", bool(scr["pumpfun_enabled"]))
            if "pumpfun_top10_max_pct" in scr:
                object.__setattr__(cfg.screening, "pumpfun_top10_max_pct", float(scr["pumpfun_top10_max_pct"]))
            if "pumpfun_min_curve_mcap_usd" in scr:
                object.__setattr__(cfg.screening, "pumpfun_min_curve_mcap_usd", float(scr["pumpfun_min_curve_mcap_usd"]))
            if "pumpfun_min_age_sec" in scr:
                object.__setattr__(cfg.screening, "pumpfun_min_age_sec", int(scr["pumpfun_min_age_sec"]))
            if "pumpfun_block_bundle_risks" in scr:
                object.__setattr__(cfg.screening, "pumpfun_block_bundle_risks", bool(scr["pumpfun_block_bundle_risks"]))
            if "pumpfun_max_m5_dump_pct" in scr:
                val_dump = float(scr["pumpfun_max_m5_dump_pct"])
                object.__setattr__(cfg.screening, "pumpfun_max_m5_dump_pct", -abs(val_dump))

        # 4. Scoring
        sco = payload.get("scoring") or {}
        if sco:
            if "threshold" in sco:
                object.__setattr__(cfg.scoring, "threshold", max(0.1, min(0.99, float(sco["threshold"]))))
            new_weights = dict(cfg.scoring.weights)
            for wk, field_name in (
                ("price_momentum", "w_price_momentum"),
                ("volume_liquidity_ratio", "w_volume_liquidity_ratio"),
                ("holder_growth", "w_holder_growth"),
                ("buy_sell_ratio", "w_buy_sell_ratio"),
                ("token_age", "w_token_age"),
            ):
                if field_name in sco:
                    new_weights[wk] = max(0.01, float(sco[field_name]))
            total_w = sum(new_weights.values())
            if total_w > 0:
                new_weights = {k: round(v / total_w, 4) for k, v in new_weights.items()}
            object.__setattr__(cfg.scoring, "weights", new_weights)

        # 5. Position & Take Profit
        pos = payload.get("position") or {}
        if pos:
            if "max_position_pct" in pos:
                object.__setattr__(cfg.position, "max_position_pct", max(0.1, float(pos["max_position_pct"])))
            if "max_position_sol" in pos:
                object.__setattr__(cfg.position, "max_position_sol", max(0.001, float(pos["max_position_sol"])))
            if "max_open_positions" in pos:
                object.__setattr__(cfg.position, "max_open_positions", max(1, int(pos["max_open_positions"])))
            if "stop_loss_pct" in pos:
                sl_val = float(pos["stop_loss_pct"])
                object.__setattr__(cfg.position, "stop_loss_pct", -abs(sl_val))
            if "trail_activation_pct" in pos:
                object.__setattr__(cfg.position.trailing_stop, "activation_pct", abs(float(pos["trail_activation_pct"])))
            if "trail_pct" in pos:
                object.__setattr__(cfg.position.trailing_stop, "trail_pct", abs(float(pos["trail_pct"])))
            if "time_stop_minutes" in pos:
                object.__setattr__(cfg.position, "time_stop_minutes", max(1, int(pos["time_stop_minutes"])))
            if "emergency_liq_drop_pct" in pos:
                object.__setattr__(cfg.position.emergency, "liquidity_drop_pct", abs(float(pos["emergency_liq_drop_pct"])))
            if all(k in pos for k in ("tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct", "tp3_pct", "tp3_sell_pct")):
                new_tps = [
                    TakeProfitLevel(pct=float(pos["tp1_pct"]), sell_pct=float(pos["tp1_sell_pct"])),
                    TakeProfitLevel(pct=float(pos["tp2_pct"]), sell_pct=float(pos["tp2_sell_pct"])),
                    TakeProfitLevel(pct=float(pos["tp3_pct"]), sell_pct=float(pos["tp3_sell_pct"])),
                ]
                object.__setattr__(cfg.position, "take_profit_levels", new_tps)

        # 6. Execution
        exe = payload.get("execution") or {}
        if exe:
            if "max_slippage_bps" in exe:
                object.__setattr__(cfg.execution, "max_slippage_bps", max(10, int(exe["max_slippage_bps"])))
            if "priority_fee_strategy" in exe:
                object.__setattr__(cfg.execution, "priority_fee_strategy", str(exe["priority_fee_strategy"]))
            if "priority_fee_fixed_lamports" in exe:
                object.__setattr__(cfg.execution, "priority_fee_fixed_lamports", max(0, int(exe["priority_fee_fixed_lamports"])))
            if "use_jito_bundle" in exe:
                object.__setattr__(cfg.execution, "use_jito_bundle", bool(exe["use_jito_bundle"]))
            if "jito_tip_lamports" in exe:
                object.__setattr__(cfg.execution, "jito_tip_lamports", max(0, int(exe["jito_tip_lamports"])))
            if "paper_sol_price_usd" in exe:
                object.__setattr__(cfg.execution, "paper_sol_price_usd", max(1.0, float(exe["paper_sol_price_usd"])))

        # 7. Risk
        rsk = payload.get("risk") or {}
        if rsk:
            if "daily_loss_limit_sol" in rsk:
                object.__setattr__(cfg.risk, "daily_loss_limit_sol", max(0.01, float(rsk["daily_loss_limit_sol"])))
            if "max_drawdown_pct" in rsk:
                object.__setattr__(cfg.risk, "max_drawdown_pct", max(1.0, float(rsk["max_drawdown_pct"])))
            if "consecutive_failures" in rsk:
                object.__setattr__(cfg.risk.circuit_breaker, "consecutive_failures", max(1, int(rsk["consecutive_failures"])))
            if "pause_duration_sec" in rsk:
                object.__setattr__(cfg.risk.circuit_breaker, "pause_duration_sec", max(10, int(rsk["pause_duration_sec"])))

        # 8. Learning
        lrn = payload.get("learning") or {}
        if lrn:
            if "auto_apply_in_paper" in lrn:
                en = bool(lrn["auto_apply_in_paper"])
                object.__setattr__(cfg.learning, "auto_apply_in_paper", en)
                object.__setattr__(cfg.learning, "require_manual_approval", not en)
            if "min_trades_for_proposal" in lrn:
                object.__setattr__(cfg.learning, "min_trades_for_proposal", max(5, int(lrn["min_trades_for_proposal"])))
            if "max_param_change_pct" in lrn:
                object.__setattr__(cfg.learning, "max_param_change_pct", max(1.0, float(lrn["max_param_change_pct"])))

        # 9. Server / .env & Telegram
        senv = payload.get("server_env") or {}
        env_to_write: dict[str, str] = {}
        if senv:
            if "telegram_enabled" in senv:
                object.__setattr__(cfg.telegram, "enabled", bool(senv["telegram_enabled"]))
            if senv.get("solana_rpc_url"):
                val = str(senv["solana_rpc_url"]).strip()
                object.__setattr__(cfg.secrets, "solana_rpc_url", val)
                env_to_write["SOLANA_RPC_URL"] = val
                os.environ["SOLANA_RPC_URL"] = val
            if "helius_ws_url" in senv and senv["helius_ws_url"] is not None:
                val = str(senv["helius_ws_url"]).strip()
                object.__setattr__(cfg.secrets, "helius_ws_url", val)
                env_to_write["HELIUS_WS_URL"] = val
                os.environ["HELIUS_WS_URL"] = val
            if "telegram_chat_id" in senv and senv["telegram_chat_id"] is not None:
                val = str(senv["telegram_chat_id"]).strip()
                object.__setattr__(cfg.secrets, "telegram_chat_id", val)
                env_to_write["TELEGRAM_CHAT_ID"] = val
                os.environ["TELEGRAM_CHAT_ID"] = val
            for secret_field, env_name in (
                ("helius_api_key", "HELIUS_API_KEY"),
                ("birdeye_api_key", "BIRDEYE_API_KEY"),
                ("wallet_private_key", "WALLET_PRIVATE_KEY"),
                ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
            ):
                raw_sec = senv.get(secret_field)
                if raw_sec and isinstance(raw_sec, str) and not raw_sec.startswith("*"):
                    clean_sec = raw_sec.strip()
                    object.__setattr__(cfg.secrets, secret_field, clean_sec)
                    env_to_write[env_name] = clean_sec
                    os.environ[env_name] = clean_sec
            if env_to_write:
                try:
                    self._update_env_file(env_to_write)
                except Exception as e:
                    logger.warning("Could not write .env file: %s", e)

            # Hot-reload notifier immediately in memory!
            if hasattr(self.bot, "notifier"):
                self.bot.notifier.reload_config(cfg)

        # 10. Persist full structure back to config.yaml
        if self.config_path.exists():
            try:
                raw_yaml = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                raw_yaml.setdefault("bot", {})["mode"] = cfg.mode.value
                raw_yaml["bot"]["live_confirm_required"] = cfg.live_confirm_required

                det_y = raw_yaml.setdefault("detection", {})
                det_y["poll_interval_sec"] = cfg.detection.poll_interval_sec
                det_y.setdefault("sources", {}).setdefault("dexscreener", {})["enabled"] = cfg.detection.dexscreener_enabled
                det_y["sources"].setdefault("birdeye", {})["enabled"] = cfg.detection.birdeye_enabled
                det_y["sources"].setdefault("helius_ws", {})["enabled"] = cfg.detection.helius_ws_enabled
                filt_y = det_y.setdefault("filters", {})
                filt_y["min_liquidity_usd"] = cfg.detection.min_liquidity_usd
                filt_y["max_token_age_sec"] = cfg.detection.max_token_age_sec
                filt_y["min_volume_usd"] = cfg.detection.min_volume_usd

                scr_y = raw_yaml.setdefault("screening", {})
                scr_y["top10_holder_max_pct"] = cfg.screening.top10_holder_max_pct
                scr_y["min_liquidity_usd"] = cfg.screening.min_liquidity_usd
                scr_y["lp_must_be_burned_or_locked"] = cfg.screening.lp_must_be_burned_or_locked
                scr_y["max_sell_tax_pct"] = cfg.screening.max_sell_tax_pct
                scr_y["max_transfer_fee_pct"] = cfg.screening.max_transfer_fee_pct
                scr_y.setdefault("rugcheck", {})["enabled"] = cfg.screening.rugcheck_enabled
                scr_y["rugcheck"]["min_score"] = cfg.screening.rugcheck_min_score
                scr_y.setdefault("deployer_check", {})["enabled"] = cfg.screening.deployer_check_enabled
                scr_y["deployer_check"]["max_rugged_deploys"] = cfg.screening.deployer_max_rugged
                pf_y = scr_y.setdefault("pumpfun", {})
                pf_y["enabled"] = cfg.screening.pumpfun_enabled
                pf_y["top10_holder_max_pct"] = cfg.screening.pumpfun_top10_max_pct
                pf_y["min_curve_mcap_usd"] = cfg.screening.pumpfun_min_curve_mcap_usd
                pf_y["min_age_sec"] = cfg.screening.pumpfun_min_age_sec
                pf_y["block_bundle_risks"] = cfg.screening.pumpfun_block_bundle_risks
                pf_y["max_m5_dump_pct"] = cfg.screening.pumpfun_max_m5_dump_pct

                sco_y = raw_yaml.setdefault("scoring", {})
                sco_y["threshold"] = cfg.scoring.threshold
                sco_y["weights"] = dict(cfg.scoring.weights)

                exe_y = raw_yaml.setdefault("execution", {})
                exe_y["max_slippage_bps"] = cfg.execution.max_slippage_bps
                exe_y.setdefault("priority_fee", {})["strategy"] = cfg.execution.priority_fee_strategy
                exe_y["priority_fee"]["fixed_lamports"] = cfg.execution.priority_fee_fixed_lamports
                exe_y["use_jito_bundle"] = cfg.execution.use_jito_bundle
                exe_y["jito_tip_lamports"] = cfg.execution.jito_tip_lamports
                exe_y["paper_sol_price_usd"] = cfg.execution.paper_sol_price_usd

                pos_y = raw_yaml.setdefault("position", {})
                pos_y["max_position_pct"] = cfg.position.max_position_pct
                pos_y["max_position_sol"] = cfg.position.max_position_sol
                pos_y["max_open_positions"] = cfg.position.max_open_positions
                pos_y["stop_loss_pct"] = cfg.position.stop_loss_pct
                pos_y["time_stop_minutes"] = cfg.position.time_stop_minutes
                pos_y.setdefault("trailing_stop", {})["activation_pct"] = cfg.position.trailing_stop.activation_pct
                pos_y["trailing_stop"]["trail_pct"] = cfg.position.trailing_stop.trail_pct
                pos_y.setdefault("emergency", {})["liquidity_drop_pct"] = cfg.position.emergency.liquidity_drop_pct
                pos_y["take_profit"] = [
                    {"pct": tp.pct, "sell_pct": tp.sell_pct}
                    for tp in cfg.position.take_profit_levels
                ]

                rsk_y = raw_yaml.setdefault("risk", {})
                rsk_y["daily_loss_limit_sol"] = cfg.risk.daily_loss_limit_sol
                rsk_y["max_drawdown_pct"] = cfg.risk.max_drawdown_pct
                rsk_y.setdefault("circuit_breaker", {})["consecutive_failures"] = cfg.risk.circuit_breaker.consecutive_failures
                rsk_y["circuit_breaker"]["pause_duration_sec"] = cfg.risk.circuit_breaker.pause_duration_sec

                raw_yaml.setdefault("telegram", {})["enabled"] = cfg.telegram.enabled

                lrn_y = raw_yaml.setdefault("learning", {})
                lrn_y["auto_apply_in_paper"] = cfg.learning.auto_apply_in_paper
                lrn_y["require_manual_approval"] = cfg.learning.require_manual_approval
                lrn_y["min_trades_for_proposal"] = cfg.learning.min_trades_for_proposal
                lrn_y["max_param_change_pct"] = cfg.learning.max_param_change_pct

                self.config_path.write_text(
                    yaml.safe_dump(raw_yaml, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning("Failed to save full config.yaml: %s", e)

        return {
            "settings": self.get_all_settings(),
            "message": "✅ Semua pengaturan berhasil disimpan ke Server (config.yaml & .env) dan langsung aktif!",
        }

    def get_dashboard_state(self) -> dict[str, Any]:
        """Build complete JSON state for the Web UI using SQLite + live in-memory bot state."""
        eq = self.bot.get_live_equity_summary()
        db_path = getattr(self.bot.db, "db_path", None) or getattr(self.bot.db, "_path", None)

        closed_trades: list[dict[str, Any]] = []
        recent_decisions: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        token_info_map: dict[str, dict[str, Any]] = {}
        latest_scored: dict[str, Any] | None = None
        active_version = "v1.0"
        gate_failure_counts: dict[str, int] = {}
        skip_screening_count = 0
        skip_score_count = 0
        sol_usd = self.bot.config.execution.paper_sol_price_usd

        if db_path and db_path != ":memory:" and Path(db_path).exists():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                # Build token lookup (symbol, name, logo_url, mcap_usd, dex from tokens)
                for trow in conn.execute(
                    "SELECT mint_address, symbol, name, dex, initial_mcap_usd, metadata_json FROM tokens"
                ):
                    mint_addr = trow["mint_address"]
                    sym = trow["symbol"] or mint_addr[:6]
                    tname = trow["name"] or sym
                    mcap_val = float(trow["initial_mcap_usd"] or 0.0)
                    dex_name = trow["dex"] or "Raydium"
                    logo_url = ""
                    if trow["metadata_json"]:
                        try:
                            meta = json.loads(trow["metadata_json"])
                            pair_meta = meta.get("pair", {})
                            logo_url = pair_meta.get("info", {}).get("imageUrl") or ""
                            if not mcap_val and pair_meta.get("fdv"):
                                mcap_val = float(pair_meta["fdv"])
                        except Exception:
                            logo_url = ""
                    if not logo_url and len(mint_addr) >= 32:
                        logo_url = f"https://dd.dexscreener.com/ds-data/tokens/solana/{mint_addr}.png"
                    token_info_map[mint_addr] = {
                        "symbol": sym,
                        "name": tname,
                        "logo_url": logo_url,
                        "mcap_usd": mcap_val or 185000.0,
                        "dex": dex_name,
                    }

                # Latest scored token for Analyst circular gauge card
                cur_sc = conn.execute(
                    """
                    SELECT sr.total_score, sr.threshold_used, sr.above_threshold, t.mint_address, t.symbol
                    FROM scoring_results sr
                    JOIN tokens t ON t.id = sr.token_id
                    ORDER BY sr.id DESC LIMIT 1
                    """
                ).fetchone()
                if cur_sc:
                    sc_mint = cur_sc["mint_address"]
                    sc_info = token_info_map.get(sc_mint, {})
                    latest_scored = {
                        "mint_address": sc_mint,
                        "symbol": sc_info.get("symbol") or cur_sc["symbol"] or sc_mint[:6],
                        "logo_url": sc_info.get("logo_url") or "",
                        "score_pct": int(round(float(cur_sc["total_score"] or 0.0) * 100)),
                        "threshold_pct": int(round(float(cur_sc["threshold_used"] or 0.65) * 100)),
                        "passed": bool(cur_sc["above_threshold"]),
                    }

                # Closed trades (chronological for curve, reverse for table)
                target_entry_sol = self.bot.initial_capital_sol * (
                    self.bot.config.position.max_position_pct / 100.0
                )
                cur = conn.execute(
                    "SELECT * FROM trades WHERE status = 'closed' ORDER BY id ASC"
                )
                for r in cur.fetchall():
                    td = dict(r)
                    raw_entry_amt = float(td.get("entry_amount_sol") or 0.0)
                    td["pnl_sol"] = self.bot.get_scaled_trade_pnl_sol(td)
                    if (
                        raw_entry_amt > 0
                        and target_entry_sol > 0
                        and raw_entry_amt > target_entry_sol * 1.5
                    ):
                        td["entry_amount_sol"] = round(target_entry_sol, 6)
                    tinfo = token_info_map.get(td.get("mint_address", ""), {})
                    td["symbol"] = tinfo.get("symbol") or td.get("mint_address", "")[:6]
                    td["name"] = tinfo.get("name") or td["symbol"]
                    td["logo_url"] = tinfo.get("logo_url") or ""
                    td["mcap_usd"] = float(tinfo.get("mcap_usd") or 185000.0)
                    td["dex"] = tinfo.get("dex") or "Raydium"
                    closed_trades.append(td)

                # Active config version
                cur_v = conn.execute(
                    "SELECT version_tag FROM config_versions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if cur_v:
                    active_version = cur_v["version_tag"]

                # Recent decisions
                cur_d = conn.execute(
                    "SELECT * FROM decisions_log ORDER BY id DESC LIMIT 40"
                )
                for r in cur_d.fetchall():
                    dd = dict(r)
                    tinfo = token_info_map.get(dd.get("mint_address", ""), {})
                    dd["symbol"] = tinfo.get("symbol") or (dd.get("mint_address") or "")[:6]
                    dd["name"] = tinfo.get("name") or dd["symbol"]
                    dd["logo_url"] = tinfo.get("logo_url") or ""
                    recent_decisions.append(dd)

                # Skip stats
                for r in conn.execute("SELECT decision_type, reason FROM decisions_log"):
                    dt = r["decision_type"]
                    if dt == "skip_screening":
                        skip_screening_count += 1
                        for part in (r["reason"] or "").split(";"):
                            gate = part.split(":")[0].strip()
                            if gate:
                                gate_failure_counts[gate] = gate_failure_counts.get(gate, 0) + 1
                    elif dt == "skip_score":
                        skip_score_count += 1

                # Optimization proposals
                cur_p = conn.execute(
                    "SELECT * FROM optimization_proposals ORDER BY id DESC LIMIT 10"
                )
                for row in cur_p.fetchall():
                    d = dict(row)
                    try:
                        d["proposed_changes"] = (
                            json.loads(d["proposed_changes"])
                            if isinstance(d["proposed_changes"], str)
                            else d["proposed_changes"]
                        )
                    except Exception:
                        d["proposed_changes"] = {}
                    proposals.append(d)
            finally:
                conn.close()

        # Build Equity Growth Curve points with timestamps & rich callout metadata for chart pills
        now_ts = datetime.now(timezone.utc).timestamp()

        def _parse_iso_ts(val: str | None, fallback_ts: float) -> float:
            if not val:
                return fallback_ts
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
            except Exception:
                return fallback_ts

        first_ts = (
            _parse_iso_ts(
                closed_trades[0].get("entry_at") or closed_trades[0].get("created_at"),
                now_ts - 86400,
            )
            - 300
            if closed_trades
            else now_ts - 3600
        )

        curve_points: list[dict[str, Any]] = [
            {
                "index": 0,
                "label": "Start",
                "timestamp": round(first_ts, 1),
                "equity_sol": round(self.bot.initial_capital_sol, 4),
                "equity_usd": round(self.bot.initial_capital_sol * sol_usd, 2),
                "pnl_sol": 0.0,
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
                "symbol": "",
                "logo_url": "",
                "action": "start",
            }
        ]
        running_eq = self.bot.initial_capital_sol
        total_closed = len(closed_trades)
        for idx, t in enumerate(closed_trades, start=1):
            pnl = float(t.get("pnl_sol") or 0.0)
            pnl_pct_t = float(t.get("pnl_pct") or 0.0)
            running_eq += pnl
            # Spread synthetic trades chronologically if timestamps are identical
            raw_ts = _parse_iso_ts(
                t.get("exit_at") or t.get("entry_at") or t.get("created_at"),
                now_ts - (total_closed - idx + 1) * 900,
            )
            curve_points.append(
                {
                    "index": idx,
                    "label": f"T#{t['id']}",
                    "timestamp": round(raw_ts, 1),
                    "equity_sol": round(running_eq, 4),
                    "equity_usd": round(running_eq * sol_usd, 2),
                    "pnl_sol": round(pnl, 4),
                    "pnl_usd": round(pnl * sol_usd, 2),
                    "pnl_pct": round(pnl_pct_t, 1),
                    "symbol": t.get("symbol") or "",
                    "logo_url": t.get("logo_url") or "",
                    "action": "win" if pnl >= 0 else "stop",
                }
            )

        # Append live mark-to-market point if open positions exist
        if eq["open_positions"]:
            lead_pos = eq["open_positions"][-1]
            lead_info = token_info_map.get(lead_pos.get("mint_address", ""), {})
            curve_points.append(
                {
                    "index": len(curve_points),
                    "label": "LIVE",
                    "timestamp": round(now_ts, 1),
                    "equity_sol": round(eq["live_equity_sol"], 4),
                    "equity_usd": round(eq["live_equity_usd"], 2),
                    "pnl_sol": round(eq["unrealized_pnl_sol"], 4),
                    "pnl_usd": round(eq["unrealized_pnl_sol"] * sol_usd, 2),
                    "pnl_pct": round(float(lead_pos.get("pnl_pct") or 0.0), 1),
                    "symbol": lead_info.get("symbol") or lead_pos.get("symbol") or "",
                    "logo_url": lead_info.get("logo_url") or "",
                    "action": "buy",
                }
            )

        metrics = compute_performance_metrics(
            closed_trades, initial_capital_sol=self.bot.initial_capital_sol
        )
        total_pnl_sol = eq["live_equity_sol"] - eq["initial_capital_sol"]
        roi_pct = (
            (total_pnl_sol / eq["initial_capital_sol"] * 100.0)
            if eq["initial_capital_sol"] > 0
            else 0.0
        )

        # Enrich open positions with entry_at, logo_url, stop_at_entry, mcap_usd, USD value
        open_pos_list = []
        for p in eq["open_positions"]:
            pos_obj = self.bot.position_manager.open_positions.get(p["trade_id"])
            p_copy = dict(p)
            if pos_obj and getattr(pos_obj, "entry_timestamp", None):
                p_copy["entry_at"] = datetime.fromtimestamp(
                    pos_obj.entry_timestamp, tz=timezone.utc
                ).isoformat()
            else:
                p_copy["entry_at"] = ""
            p_copy["remaining_sol"] = float(p_copy.get("entry_sol", 0.0))
            p_copy["entry_usd"] = round(p_copy["remaining_sol"] * sol_usd, 2)
            p_copy["pnl_usd"] = round(float(p_copy.get("pnl_sol") or 0.0) * sol_usd, 2)
            p_copy["current_value_usd"] = round(
                (p_copy["remaining_sol"] + float(p_copy.get("pnl_sol") or 0.0)) * sol_usd, 2
            )
            p_copy["stop_at_entry"] = bool(getattr(pos_obj, "stop_at_entry", False)) if pos_obj else False
            p_copy["stop_loss_pct"] = 0.0 if p_copy["stop_at_entry"] else self.bot.config.position.stop_loss_pct
            tinfo = token_info_map.get(p_copy.get("mint_address", ""), {})
            if tinfo.get("symbol"):
                p_copy["symbol"] = tinfo["symbol"]
            p_copy["name"] = tinfo.get("name") or p_copy.get("symbol", "")
            p_copy["logo_url"] = tinfo.get("logo_url") or ""
            p_copy["mcap_usd"] = float(tinfo.get("mcap_usd") or 185000.0)
            p_copy["dex"] = tinfo.get("dex") or "Raydium"
            open_pos_list.append(p_copy)

        next_entry_sol = min(
            round((self.bot.config.position.max_position_pct / 100.0) * eq["live_equity_sol"], 6),
            self.bot.config.position.max_position_sol,
            eq["cash_available_sol"],
        )

        return {
            "mode": self.bot.config.mode.value,
            "wallet_configured": bool(self.bot.config.secrets.wallet_private_key),
            "bot": {
                "is_running": self.is_running,
                "cycle_count": self.cycle_count,
                "poll_interval_sec": self.bot.config.detection.poll_interval_sec,
            },
            "equity": {
                "initial_capital_sol": eq["initial_capital_sol"],
                "live_equity_sol": eq["live_equity_sol"],
                "live_equity_usd": eq["live_equity_usd"],
                "cash_available_sol": eq["cash_available_sol"],
                "locked_sol": eq["locked_sol"],
                "unrealized_pnl_sol": eq["unrealized_pnl_sol"],
                "total_pnl_sol": round(total_pnl_sol, 4),
                "roi_pct": round(roi_pct, 2),
                "drawdown_pct": eq["drawdown_pct"],
                "max_position_pct": self.bot.config.position.max_position_pct,
                "next_entry_sol": round(next_entry_sol, 4),
            },
            "performance": {
                "trades_count": metrics.trades_count,
                "wins": metrics.wins,
                "losses": metrics.losses,
                "win_rate": round(metrics.win_rate, 2),
                "expectancy": round(metrics.expectancy, 5),
                "profit_factor": round(metrics.profit_factor, 4),
                "net_pnl_sol": round(metrics.net_pnl_sol, 4),
                "max_drawdown_pct": round(metrics.max_drawdown_pct, 2),
            },
            "risk": {
                "can_trade": not self.bot.risk_controller.is_halted
                and not self.bot.risk_controller.kill_switch.is_active()[0],
                "halt_reason": self.bot.risk_controller.halt_reason or "Kill switch aktif",
                "kill_switch_active": self.bot.risk_controller.kill_switch.is_active()[0],
                "max_drawdown_pct": self.bot.config.risk.max_drawdown_pct,
                "daily_loss_limit_sol": self.bot.config.risk.daily_loss_limit_sol,
                "max_open_positions": self.bot.config.position.max_open_positions,
                "stop_loss_pct": self.bot.config.position.stop_loss_pct,
                "trail_pct": self.bot.config.position.trailing_stop.trail_pct,
            },
            "latest_scored": latest_scored,
            "sol_price_usd": sol_usd,
            "equity_curve": curve_points,
            "open_positions": open_pos_list,
            "closed_trades": list(reversed(closed_trades)),
            "recent_decisions": recent_decisions,
            "skip_summary": {
                "skip_screening_count": skip_screening_count,
                "skip_score_count": skip_score_count,
                "total_skipped": skip_screening_count + skip_score_count,
                "gate_failure_counts": gate_failure_counts,
            },
            "learning": {
                "active_version": active_version,
                "auto_apply_in_paper": self.bot.config.learning.auto_apply_in_paper,
                "tunable_params": {
                    k: v
                    for k, v in self.bot.optimizer.get_current_tunable_params().items()
                    if isinstance(v, (int, float))
                },
                "proposals": proposals,
            },
            "settings": self.get_all_settings(),
        }


def make_handler(controller: BotUIController) -> type[BaseHTTPRequestHandler]:
    """Create a request handler class bound to `controller`."""

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence noisy HTTP polling logs
            pass

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                html_bytes = DASHBOARD_HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            if self.path.startswith("/api/state"):
                try:
                    state = controller.get_dashboard_state()
                    self._send_json(state)
                except Exception as e:
                    logger.error("Error building dashboard state: %s", e)
                    self._send_json({"error": str(e)}, status=500)
                return

            if self.path.startswith("/api/settings"):
                try:
                    self._send_json({"settings": controller.get_all_settings()})
                except Exception as e:
                    self._send_json({"error": str(e)}, status=500)
                return

            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            try:
                body = self._read_json_body()
                if self.path == "/api/mode":
                    res = controller.switch_mode(
                        target_mode=str(body.get("mode", "PAPER")),
                        confirmation_phrase=str(body.get("confirmation_phrase", "")),
                    )
                    self._send_json(res)
                elif self.path == "/api/bot/toggle":
                    self._send_json(controller.toggle_engine())
                elif self.path == "/api/bot/scan-once":
                    self._send_json(controller.scan_once())
                elif self.path == "/api/kill-switch":
                    self._send_json(
                        controller.toggle_kill_switch(str(body.get("action", "activate")))
                    )
                elif self.path == "/api/positions/close":
                    self._send_json(
                        controller.close_position(
                            body.get("trade_id", "all"),
                            sell_pct=float(body.get("sell_pct", 100.0)),
                        )
                    )
                elif self.path == "/api/positions/breakeven":
                    self._send_json(
                        controller.move_stop_to_entry(int(body.get("trade_id", 0)))
                    )
                elif self.path == "/api/learning/toggle-auto":
                    self._send_json(
                        controller.toggle_auto_learn(bool(body.get("enabled", True)))
                    )
                elif self.path == "/api/learning/run-now":
                    self._send_json(controller.run_learning_now())
                elif self.path == "/api/learning/approve":
                    self._send_json(
                        controller.approve_proposal(int(body.get("proposal_id", 0)))
                    )
                elif self.path == "/api/capital":
                    self._send_json(
                        controller.set_capital(float(body.get("capital_sol", 0.0)))
                    )
                elif self.path == "/api/settings":
                    self._send_json(controller.update_all_settings(body))
                else:
                    self._send_json({"error": f"Unknown endpoint {self.path}"}, status=404)
            except ValueError as ve:
                self._send_json({"error": str(ve)}, status=400)
            except Exception as e:
                logger.error("UI POST error on %s: %s", self.path, e)
                self._send_json({"error": str(e)}, status=500)

    return DashboardRequestHandler


def start_background_ui_server(
    controller: BotUIController,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Start the Web UI HTTP server on a background daemon thread."""
    handler_cls = make_handler(controller)
    server = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="BotWebUIServer")
    thread.start()
    logger.info("Web UI Dashboard running at http://%s:%d", host, port)
    return server


async def _standalone_main() -> None:
    parser = argparse.ArgumentParser(description="Solana Meme Bot — Web UI Dashboard Server")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--capital", type=float, default=10.0, help="Initial capital in SOL")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Immediately start auto-scanning token loop on launch",
    )
    args = parser.parse_args()

    from src.logging_.trade_logger import setup_logging

    config = load_config(config_path=args.config)
    setup_logging(config)

    bot = MemeTradingBot(config=config, initial_capital_sol=args.capital)
    await bot.initialize()

    loop = asyncio.get_running_loop()
    controller = BotUIController(
        bot=bot,
        loop=loop,
        config_path=args.config,
        is_running=False,
    )

    server = start_background_ui_server(controller, host=args.host, port=args.port)
    print(
        "\n" + "=" * 74 + "\n"
        f"  🌐 SOLANA MEME BOT — WEB UI DASHBOARD AKTIF\n"
        f"  👉 Buka di Browser: http://{args.host}:{args.port}\n"
        f"  Mode Saat Ini     : {config.mode.value} | Modal Awal: {args.capital:.2f} SOL\n"
        + "=" * 74 + "\n",
        flush=True,
    )

    if args.auto_start:
        controller.toggle_engine()

    try:
        while True:
            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(_standalone_main())
