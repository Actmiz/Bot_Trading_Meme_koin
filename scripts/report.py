"""Generate a complete performance, risk, and screening skip report from the SQLite database."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import load_config
from src.core.database import Database
from src.learning.backtester import compute_performance_metrics
from src.logging_.trade_logger import TradeAuditLogger


async def generate_report_text(db: Database, config, initial_capital_sol: float = 10.0) -> str:
    """Build a formatted text report of trading performance, skip stats, and proposals."""
    closed_trades = await db.get_all_closed_trades()
    open_trades = await db.get_open_trades()
    metrics = compute_performance_metrics(closed_trades, initial_capital_sol=initial_capital_sol)

    audit = TradeAuditLogger(config=config, database=db)
    skip_summary = await audit.get_skip_summary()
    await audit.notifier.close()

    active_ver = await db.get_active_config_version()
    pending_props = await db.get_pending_proposals()

    locked_sol = sum(float(t.get("entry_amount_sol", 0.0) or 0.0) for t in open_trades)
    realized_pnl_sol = metrics.net_pnl_sol
    live_equity_sol = initial_capital_sol + realized_pnl_sol
    free_balance_sol = max(0.0, live_equity_sol - locked_sol)
    roi_pct = (realized_pnl_sol / initial_capital_sol * 100.0) if initial_capital_sol > 0 else 0.0

    lines = [
        "=" * 68,
        "  SOLANA MEME BOT — LAPORAN PERFORMA & LIVE EQUITY",
        "=" * 68,
        f"  Mode Konfigurasi     : {config.mode.value}",
        f"  Versi Config Aktif   : {active_ver['version_tag'] if active_ver else 'v1.0 (default)'}",
        f"  Modal Awal           : {initial_capital_sol:.4f} SOL",
        f"  Live Equity          : {live_equity_sol:.4f} SOL ({roi_pct:+.2f}%)",
        f"  Saldo Bebas (Free)   : {free_balance_sol:.4f} SOL | Terkunci: {locked_sol:.4f} SOL",
        f"  Posisi Terbuka       : {len(open_trades)} / {config.position.max_open_positions}",
    ]

    if open_trades:
        lines.append("-" * 68)
        lines.append("  POSISI TERBUKA SAAT INI (ACTIVE BUYS):")
        for pos in open_trades:
            mint_short = f"{pos['mint_address'][:6]}...{pos['mint_address'][-4:]}"
            lines.append(
                f"  - Trade #{pos['id']} | Mint: {mint_short} | "
                f"Size: {float(pos.get('entry_amount_sol') or 0.0):.4f} SOL | "
                f"Entry: ${float(pos.get('entry_price_usd') or 0.0):.8f} | "
                f"Waktu Beli: {pos.get('entry_at') or pos.get('created_at')}"
            )

    lines.extend([
        "-" * 68,
        "  METRIK PERFORMA (CLOSED TRADES):",
        f"  - Total Trade Selesai: {metrics.trades_count} (Win: {metrics.wins} | Loss: {metrics.losses})",
        f"  - Win Rate           : {metrics.win_rate:.2f}%",
        f"  - Expectancy         : {metrics.expectancy:+.6f} SOL / trade",
        f"  - Profit Factor      : {metrics.profit_factor:.4f}",
        f"  - Net PnL            : {metrics.net_pnl_sol:+.4f} SOL",
        f"  - Max Drawdown       : {metrics.max_drawdown_pct:.2f}%",
        "-" * 68,
        "  STATISTIK TOKEN YANG DI-SKIP (EVALUASI FILTER):",
        f"  - Skip oleh Screening: {skip_summary['skip_screening_count']}",
        f"  - Skip oleh Scoring  : {skip_summary['skip_score_count']}",
        f"  - Total Token Di-skip: {skip_summary['total_skipped']}",
    ])

    if skip_summary["gate_failure_counts"]:
        lines.append("  - Rincian Gate Gagal :")
        for gate, cnt in sorted(
            skip_summary["gate_failure_counts"].items(), key=lambda x: x[1], reverse=True
        ):
            lines.append(f"      * {gate}: {cnt}")

    lines.append("-" * 68)
    lines.append(f"  USULAN PARAMETER MENUNGGU PERSETUJUAN: {len(pending_props)}")
    for p in pending_props:
        lines.append(
            f"  - Proposal #{p['id']} | Validated={bool(p['passed_validation'])} | "
            f"Changes={p['proposed_changes']}"
        )
    lines.append("=" * 68)
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Print trading performance & skip audit report")
    parser.add_argument("--db", default=None, help="Override SQLite database path")
    parser.add_argument("--capital", type=float, default=10.0, help="Initial capital in SOL")
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        help="Auto-refresh report every N seconds (0 = print once and exit)",
    )
    args = parser.parse_args()

    config = load_config()
    db = Database(config=config.database, db_path=args.db)
    await db.connect()
    try:
        if args.watch <= 0:
            report_str = await generate_report_text(db, config, initial_capital_sol=args.capital)
            print(report_str)
        else:
            while True:
                report_str = await generate_report_text(db, config, initial_capital_sol=args.capital)
                print("\033[2J\033[H" + report_str, flush=True)
                await asyncio.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

