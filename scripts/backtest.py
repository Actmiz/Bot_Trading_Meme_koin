"""Offline Paper Trader, Walk-Forward Backtester, and Proposal Approval CLI script."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import load_config
from src.core.database import Database
from src.core.enums import BotMode, TokenSource, TradeStatus
from src.core.models import TokenSnapshot, TradeRecord
from src.learning.analyzer import PostTradeAnalyzer
from src.learning.backtester import compute_performance_metrics
from src.learning.optimizer import ParameterOptimizer


def generate_synthetic_paper_trades(count: int = 45) -> list[dict]:
    """Generate realistic paper trade history for offline backtesting and learning."""
    trades = []
    for i in range(count):
        if i % 3 == 0:
            # Marginal entry score -> loss
            feats = {
                "price_momentum": 0.66,
                "volume_liquidity_ratio": 0.65,
                "holder_growth": 0.66,
                "buy_sell_ratio": 0.66,
                "token_age": 0.66,
            }
            score = 0.658
            pnl_sol = -0.045
            pnl_pct = -22.5
            reason = "stop_loss"
        elif i % 5 == 0:
            # Small time-stop scratch loss
            feats = {
                "price_momentum": 0.67,
                "volume_liquidity_ratio": 0.67,
                "holder_growth": 0.67,
                "buy_sell_ratio": 0.67,
                "token_age": 0.67,
            }
            score = 0.670
            pnl_sol = -0.015
            pnl_pct = -7.5
            reason = "time_stop"
        else:
            # High quality momentum setup -> multi-TP / trailing stop winner
            feats = {
                "price_momentum": 0.85,
                "volume_liquidity_ratio": 0.82,
                "holder_growth": 0.78,
                "buy_sell_ratio": 0.84,
                "token_age": 0.82,
            }
            score = 0.825
            pnl_sol = 0.085
            pnl_pct = 42.5
            reason = "trailing_stop"

        trades.append(
            {
                "id": i + 1,
                "entry_score": score,
                "entry_amount_sol": 0.20,
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "entry_slippage_pct": 0.6,
                "exit_slippage_pct": 0.6,
                "exit_reason": reason,
                "holding_duration_sec": 780,
                "entry_features_json": {
                    "features": feats,
                    "raw": {"top10_holder_pct": 24.0, "token_age_seconds": 420},
                },
            }
        )
    return trades


async def _seed_synthetic_trades_into_db(db: Database, synthetic_trades: list[dict]) -> None:
    """Persist synthetic paper trades into SQLite so they can be inspected via report.py."""
    for idx, t in enumerate(synthetic_trades):
        mint = f"SimToken{idx + 1:04d}1111111111111111111111111111111"
        token = TokenSnapshot(
            mint_address=mint,
            symbol=f"SIM{idx + 1}",
            source=TokenSource.DEXSCREENER,
            initial_price_usd=0.001,
            initial_liquidity_usd=50000.0,
        )
        token_id = await db.insert_token(token)
        rec = TradeRecord(
            token_id=token_id,
            mint_address=mint,
            mode=BotMode.PAPER.value,
            entry_price_usd=0.001,
            entry_amount_sol=t["entry_amount_sol"],
            entry_amount_tokens=30000.0,
            entry_slippage_pct=t["entry_slippage_pct"],
            entry_fee_sol=0.00005,
            entry_at="2026-11-05T10:00:00Z",
            status=TradeStatus.OPEN,
            entry_features_json=t["entry_features_json"],
            entry_score=t["entry_score"],
        )
        trade_id = await db.insert_trade(rec)
        await db.update_trade_exit(
            trade_id=trade_id,
            exit_price_usd=0.0012 if t["pnl_sol"] > 0 else 0.0008,
            exit_amount_sol=t["entry_amount_sol"] + t["pnl_sol"],
            exit_slippage_pct=t["exit_slippage_pct"],
            exit_fee_sol=0.00005,
            exit_tx_signature=f"sim_exit_{trade_id}",
            exit_reason=t["exit_reason"],
            pnl_sol=t["pnl_sol"],
            pnl_usd=t["pnl_sol"] * 150.0,
            pnl_pct=t["pnl_pct"],
            holding_duration_sec=t["holding_duration_sec"],
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run Walk-Forward Backtest & Paper Learning")
    parser.add_argument("--db", default=None, help="Database path (defaults to config.database.path when --save-db or --approve is used)")
    parser.add_argument("--save-db", action="store_true", help="Save simulated paper trades & learning proposal into ./data/bot.db")
    parser.add_argument("--approve", type=int, default=None, help="Approve pending proposal ID in ./data/bot.db and activate new config version")
    parser.add_argument("--capital", type=float, default=10.0, help="Starting capital in SOL")
    parser.add_argument("--synthetic-count", type=int, default=45, help="Synthetic trades if DB has none")
    args = parser.parse_args()

    config = load_config()
    use_real_db = args.save_db or (args.approve is not None) or (args.db is not None)
    db_path = args.db if args.db else (config.database.path if use_real_db else ":memory:")

    db = Database(config=config.database, db_path=db_path)
    await db.connect()
    optimizer = ParameterOptimizer(config=config, database=db)

    # Mode 1: Approve an existing proposal in DB
    if args.approve is not None:
        ver_tag = await optimizer.approve_proposal(args.approve)
        print(f"Proposal #{args.approve} disetujui! Versi konfigurasi baru aktif: {ver_tag}")
        await db.close()
        return

    # Mode 2: Run Paper Backtest & Learning Cycle
    trades = await db.get_all_closed_trades()
    source_label = f"SQLite Database ({db_path})"
    if not trades:
        trades = generate_synthetic_paper_trades(args.synthetic_count)
        if args.save_db:
            await _seed_synthetic_trades_into_db(db, trades)
            trades = await db.get_all_closed_trades()
            source_label = f"Seeded {len(trades)} Paper Trades -> {db_path}"
        else:
            source_label = f"Synthetic Paper Dataset ({len(trades)} trades in-memory)"

    metrics = compute_performance_metrics(trades, initial_capital_sol=args.capital)
    analyzer = PostTradeAnalyzer()
    analysis = analyzer.analyze_trades(
        trades=trades,
        current_threshold=config.scoring.threshold,
        period_label="paper_learning_cycle",
        initial_capital_sol=args.capital,
    )

    _, proposal, validation = await optimizer.generate_proposal_from_trades(
        trades=trades,
        period_label="paper_learning_cycle",
    )

    print("=" * 68)
    print("  SOLANA MEME BOT — PAPER TEST & LEARNING REPORT")
    print(f"  Data Source : {source_label}")
    print("=" * 68)
    print(f"  Total Trades     : {metrics.trades_count} (Wins: {metrics.wins}, Losses: {metrics.losses})")
    print(f"  Win Rate         : {metrics.win_rate:.2f}%")
    print(f"  Expectancy       : {metrics.expectancy:+.6f} SOL / trade")
    print(f"  Profit Factor    : {metrics.profit_factor:.4f}")
    print(f"  Net PnL          : {metrics.net_pnl_sol:+.4f} SOL")
    print(f"  Max Drawdown     : {metrics.max_drawdown_pct:.2f}%")
    print("-" * 68)
    print("  POST-TRADE LOSS PATTERN ANALYSIS:")
    if analysis.patterns:
        for p in analysis.patterns:
            print(
                f"   - {p.pattern_id:<28}: {p.count} losses ({p.share_of_losses_pct:.1f}%), "
                f"avg={p.avg_loss_sol:+.4f} SOL -> {p.suggested_direction} `{p.suggested_param}`"
            )
    else:
        print("   - No recurring loss patterns found.")

    if proposal and validation:
        print("-" * 68)
        print("  WALK-FORWARD OPTIMIZATION PROPOSAL:")
        ch = proposal.proposed_changes
        print(f"   - Proposed Change : {ch['parameter']} : {ch['old_value']} -> {ch['new_value']}")
        print(f"   - WF Splits Passed: {validation.splits_improved}/{validation.total_splits}")
        print(
            f"   - OOS Expectancy  : {validation.baseline_metrics.expectancy:+.4f} SOL -> "
            f"{validation.proposed_metrics.expectancy:+.4f} SOL ({validation.improvement_pct:+.1f}%)"
        )
        print(f"   - Validation Pass : {validation.passed} (Status: {proposal.status.value})")
    print("=" * 68)

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
