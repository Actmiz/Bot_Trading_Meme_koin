"""Unit tests for the Web UI Dashboard controller and mode switching gates."""

import asyncio
import pytest

from src.bot import LIVE_CONFIRMATION_PHRASE, MemeTradingBot
from src.core.config import BotConfig, DatabaseConfig
from src.core.enums import BotMode
from src.ui.server import BotUIController


@pytest.mark.asyncio
async def test_ui_controller_state_and_mode_switch(tmp_path):
    db_file = tmp_path / "ui_test.db"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "bot:\n  mode: PAPER\n  live_confirm_required: true\nlearning:\n  auto_apply_in_paper: true\n  require_manual_approval: false\n",
        encoding="utf-8",
    )

    config = BotConfig(
        mode=BotMode.PAPER,
        database=DatabaseConfig(path=str(db_file)),
    )
    bot = MemeTradingBot(config=config, initial_capital_sol=10.0)
    await bot.initialize()

    loop = asyncio.get_running_loop()
    controller = BotUIController(bot=bot, loop=loop, config_path=cfg_file, is_running=False)

    try:
        # 1. Verify initial dashboard state
        state = controller.get_dashboard_state()
        assert state["mode"] == "PAPER"
        assert state["equity"]["initial_capital_sol"] == 10.0
        assert state["equity"]["live_equity_sol"] == 10.0
        assert len(state["equity_curve"]) >= 1

        # 2. Switching to LIVE without exact phrase must fail
        with pytest.raises(ValueError, match="Frasa konfirmasi salah"):
            controller.switch_mode("LIVE", confirmation_phrase="salah")

        # 3. Switching to LIVE with exact phrase succeeds and updates config.yaml
        res_live = controller.switch_mode("LIVE", confirmation_phrase=LIVE_CONFIRMATION_PHRASE)
        assert res_live["mode"] == "LIVE"
        assert bot.config.mode == BotMode.LIVE
        assert "mode: LIVE" in cfg_file.read_text(encoding="utf-8")

        # 4. Switching back to PAPER succeeds immediately
        res_paper = controller.switch_mode("PAPER")
        assert res_paper["mode"] == "PAPER"
        assert bot.config.mode == BotMode.PAPER
        assert "mode: PAPER" in cfg_file.read_text(encoding="utf-8")

        # 5. Toggle auto-learn
        res_learn = controller.toggle_auto_learn(False)
        assert res_learn["auto_apply_in_paper"] is False
        assert bot.config.learning.auto_apply_in_paper is False

        # 6. Register an open position and verify get_dashboard_state() + manual close
        from src.core.enums import TokenSource
        from src.core.models import TokenSnapshot, TradeRecord

        tok_id = await bot.db.insert_token(
            TokenSnapshot(
                mint_address="BFJd5mWC11111111111111111111111111111111pump",
                symbol="PILLHEADS",
                source=TokenSource.DEXSCREENER,
                initial_price_usd=0.00023346,
                initial_liquidity_usd=25000.0,
            )
        )
        tr = TradeRecord(
            token_id=tok_id,
            mint_address="BFJd5mWC11111111111111111111111111111111pump",
            mode=BotMode.PAPER,
            entry_price_usd=0.00023346,
            entry_amount_sol=0.22,
            entry_amount_tokens=141351.0,
        )
        tr_id = await bot.db.insert_trade(tr)
        tr.id = tr_id
        bot.position_manager.register_trade(tr)
        bot._last_prices[tr.mint_address] = 0.00025000

        state_with_pos = controller.get_dashboard_state()
        assert len(state_with_pos["open_positions"]) == 1
        assert state_with_pos["open_positions"][0]["trade_id"] == tr_id
        assert state_with_pos["open_positions"][0]["entry_at"] != ""
        assert state_with_pos["open_positions"][0]["stop_at_entry"] is False

        # 7. Test "Move stop to entry" (breakeven stop) from Trading Desk context menu
        be_res = controller.move_stop_to_entry(tr_id)
        assert be_res["stop_at_entry"] is True
        assert "stop moved to entry" in be_res["message"]
        state_after_be = controller.get_dashboard_state()
        assert state_after_be["open_positions"][0]["stop_at_entry"] is True
        assert state_after_be["open_positions"][0]["stop_loss_pct"] == 0.0
    finally:
        await bot.shutdown()


@pytest.mark.asyncio
async def test_dynamic_equity_entry_sizing_with_small_capital(tmp_path):
    """Verify that 0.5 SOL capital sizes entry at 0.01 SOL (2%) and scales dynamically with equity."""
    from src.core.enums import TokenSource
    from src.core.models import TokenSnapshot

    db_file = tmp_path / "small_cap.db"
    config = BotConfig(
        mode=BotMode.PAPER,
        database=DatabaseConfig(path=str(db_file)),
    )
    bot = MemeTradingBot(config=config, initial_capital_sol=0.5)
    await bot.initialize()

    try:
        eq0 = bot.get_live_equity_summary()
        assert eq0["initial_capital_sol"] == 0.5
        assert eq0["live_equity_sol"] == 0.5

        mock_screen = {
            "mock_authorities": {"mintAuthority": None, "freezeAuthority": None},
            "mock_holders": [{"uiAmount": 10.0}],
            "mock_total_supply": 1000.0,
            "mock_lp_burned_pct": 100.0,
            "mock_simulation": {"can_sell": True, "sell_tax_pct": 0.0, "transfer_fee_pct": 0.0},
            "mock_rugcheck": {"score": 95.0, "risks": []},
            "mock_deployer": {"rug_count": 0},
        }

        # Candidate 1 at 0.5 SOL equity -> 2% of 0.5 SOL = 0.01 SOL
        tok1 = TokenSnapshot(
            mint_address="SmallCapMint11111111111111111111111111111111",
            symbol="SMOL1",
            source=TokenSource.DEXSCREENER,
            initial_price_usd=0.001,
            initial_liquidity_usd=50000.0,
            initial_volume_24h=150000.0,
            initial_holders=850,
            initial_buy_count=1200,
            initial_sell_count=600,
            token_age_seconds=600,
        )
        trade1 = await bot.process_candidate_token(
            tok1,
            price_change_5m_pct=35.0,
            mock_screening_kwargs=mock_screen,
        )
        assert trade1 is not None
        assert trade1.entry_amount_sol == pytest.approx(0.01, rel=1e-3)

        # Simulate equity increasing to 0.80 SOL -> next entry should follow live equity (2% of ~0.80 = 0.016 SOL)
        bot.risk_controller.current_capital_sol = 0.80
        tok2 = TokenSnapshot(
            mint_address="SmallCapMint22222222222222222222222222222222",
            symbol="SMOL2",
            source=TokenSource.DEXSCREENER,
            initial_price_usd=0.001,
            initial_liquidity_usd=50000.0,
            initial_volume_24h=150000.0,
            initial_holders=850,
            initial_buy_count=1200,
            initial_sell_count=600,
            token_age_seconds=600,
        )
        trade2 = await bot.process_candidate_token(
            tok2,
            price_change_5m_pct=35.0,
            mock_screening_kwargs=mock_screen,
        )
        assert trade2 is not None
        assert trade2.entry_amount_sol == pytest.approx(0.016, rel=1e-2)
    finally:
        await bot.shutdown()


@pytest.mark.asyncio
async def test_full_server_settings_update_via_ui(tmp_path):
    """Verify that all settings (config.yaml + .env) can be updated from the Web UI controller."""
    db_file = tmp_path / "settings_test.db"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("bot:\n  mode: PAPER\n", encoding="utf-8")

    config = BotConfig(
        mode=BotMode.PAPER,
        database=DatabaseConfig(path=str(db_file)),
    )
    bot = MemeTradingBot(config=config, initial_capital_sol=1.0)
    await bot.initialize()

    loop = asyncio.get_running_loop()
    controller = BotUIController(bot=bot, loop=loop, config_path=cfg_file, is_running=False)

    try:
        res = controller.update_all_settings(
            {
                "capital": {"initial_capital_sol": 0.5},
                "position": {
                    "max_position_pct": 3.0,
                    "stop_loss_pct": -20.0,
                    "tp1_pct": 40.0,
                    "tp1_sell_pct": 50.0,
                    "tp2_pct": 90.0,
                    "tp2_sell_pct": 30.0,
                    "tp3_pct": 180.0,
                    "tp3_sell_pct": 100.0,
                },
                "screening": {
                    "pumpfun_enabled": True,
                    "pumpfun_top10_max_pct": 30.0,
                    "pumpfun_min_curve_mcap_usd": 40000.0,
                },
                "scoring": {"threshold": 0.68},
                "server_env": {
                    "solana_rpc_url": "https://my-custom-rpc.example.com",
                    "wallet_private_key": "5JtestBase58PrivateKeyDoNotLeak",
                    "telegram_enabled": True,
                    "telegram_chat_id": "99887766",
                },
            }
        )
        st = res["settings"]
        assert st["capital"]["initial_capital_sol"] == 0.5
        assert st["position"]["max_position_pct"] == 3.0
        assert st["position"]["stop_loss_pct"] == -20.0
        assert st["position"]["tp1_pct"] == 40.0
        assert st["screening"]["pumpfun_top10_max_pct"] == 30.0
        assert st["scoring"]["threshold"] == 0.68
        assert st["server_env"]["solana_rpc_url"] == "https://my-custom-rpc.example.com"
        assert st["server_env"]["wallet_private_key_set"] is True
        # Ensure raw private key is NEVER returned in GET/POST response JSON
        assert "wallet_private_key" not in st["server_env"]
        assert (tmp_path / ".env").exists()
        assert "WALLET_PRIVATE_KEY=5JtestBase58PrivateKeyDoNotLeak" in (tmp_path / ".env").read_text(
            encoding="utf-8"
        )
    finally:
        await bot.shutdown()



