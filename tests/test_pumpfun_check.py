"""Unit tests for Gate 7: Pump.fun-specific safety checks."""

from src.core.enums import DexType, TokenSource
from src.core.models import TokenSnapshot
from src.screening.pumpfun_check import (
    PHASE_BONDING_CURVE,
    PHASE_GRADUATED,
    PHASE_NOT_PUMPFUN,
    check_pumpfun_safety,
    detect_pumpfun_phase,
)

PUMP_MINT = "Bu3h78FY2fsWVZ1MdyQVpFTvv5XsRotWX6VjszQTpump"
NORMAL_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _tok(mint=PUMP_MINT, dex_id="pumpswap", mcap=80000.0, age=900, m5=5.0, buys=50, sells=20):
    return TokenSnapshot(
        mint_address=mint,
        symbol="TEST",
        source=TokenSource.DEXSCREENER,
        dex=DexType.PUMPFUN if "pump" in dex_id else DexType.RAYDIUM,
        initial_mcap_usd=mcap,
        initial_liquidity_usd=20000.0,
        token_age_seconds=age,
        metadata_json={
            "pair": {
                "dexId": dex_id,
                "fdv": mcap,
                "priceChange": {"m5": m5},
                "txns": {"m5": {"buys": buys, "sells": sells}},
            }
        },
    )


def test_phase_detection():
    assert detect_pumpfun_phase(_tok(dex_id="pumpfun")) == (True, PHASE_BONDING_CURVE)
    assert detect_pumpfun_phase(_tok(dex_id="pumpswap")) == (True, PHASE_GRADUATED)
    assert detect_pumpfun_phase(_tok(mint=NORMAL_MINT, dex_id="raydium")) == (False, PHASE_NOT_PUMPFUN)


def test_non_pumpfun_token_is_ignored():
    res = check_pumpfun_safety(_tok(mint=NORMAL_MINT, dex_id="raydium"), top10_holder_pct=60.0, rugcheck_risks=[])
    assert res.is_pumpfun is False
    assert res.passed is True


def test_graduated_clean_token_passes():
    res = check_pumpfun_safety(_tok(), top10_holder_pct=22.0, rugcheck_risks=[])
    assert res.passed is True
    assert res.phase == PHASE_GRADUATED


def test_early_bonding_curve_rejected():
    res = check_pumpfun_safety(_tok(dex_id="pumpfun", mcap=12000.0), top10_holder_pct=20.0, rugcheck_risks=[])
    assert res.passed is False
    assert any("early bonding curve" in r for r in res.fail_reasons)


def test_king_of_the_hill_curve_allowed():
    res = check_pumpfun_safety(_tok(dex_id="pumpfun", mcap=50000.0), top10_holder_pct=20.0, rugcheck_risks=[])
    assert res.passed is True


def test_strict_top10_and_bundle_and_dump_rejected():
    assert not check_pumpfun_safety(_tok(), top10_holder_pct=40.0, rugcheck_risks=[]).passed
    assert not check_pumpfun_safety(
        _tok(), top10_holder_pct=20.0, rugcheck_risks=["Bundled supply [warn]: 30% bought in same slot"]
    ).passed
    assert not check_pumpfun_safety(_tok(m5=-40.0, buys=10, sells=90), top10_holder_pct=20.0, rugcheck_risks=[]).passed
    assert not check_pumpfun_safety(_tok(age=30), top10_holder_pct=20.0, rugcheck_risks=[]).passed

