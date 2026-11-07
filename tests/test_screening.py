"""Unit tests for Modul 2: Screening Keamanan (Gate Keras).

Tests each security gate individually and tests the overall SecurityScreener pipeline.
"""

import pytest

from src.core.config import BotConfig, ScreeningConfig
from src.core.database import Database
from src.core.enums import TokenSource, DexType, LPStatus, DecisionType
from src.core.models import TokenSnapshot
from src.screening.authority_check import check_authorities
from src.screening.holder_check import check_holder_concentration
from src.screening.liquidity_check import check_liquidity_and_lp
from src.screening.honeypot_check import check_honeypot_and_fees
from src.screening.rugcheck import check_rugcheck
from src.screening.deployer_check import check_deployer_history
from src.screening.screener import SecurityScreener


MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# ---------------------------------------------------------------------------
# Gate 1: Mint & Freeze Authorities
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_authority_check_active_mint():
    mock_data = {
        "mintAuthority": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "freezeAuthority": None,
    }
    mint_rev, freeze_rev, fails = await check_authorities(
        mint_address=MINT_SAMPLE,
        rpc_url="http://mock",
        parsed_account_data=mock_data,
    )
    assert mint_rev is False
    assert freeze_rev is True
    assert len(fails) == 1
    assert "Mint authority active" in fails[0]


@pytest.mark.asyncio
async def test_authority_check_active_freeze():
    mock_data = {
        "mintAuthority": None,
        "freezeAuthority": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    }
    mint_rev, freeze_rev, fails = await check_authorities(
        mint_address=MINT_SAMPLE,
        rpc_url="http://mock",
        parsed_account_data=mock_data,
    )
    assert mint_rev is True
    assert freeze_rev is False
    assert len(fails) == 1
    assert "Freeze authority active" in fails[0]


@pytest.mark.asyncio
async def test_authority_check_both_revoked():
    mock_data = {
        "mintAuthority": None,
        "freezeAuthority": None,
    }
    mint_rev, freeze_rev, fails = await check_authorities(
        mint_address=MINT_SAMPLE,
        rpc_url="http://mock",
        parsed_account_data=mock_data,
    )
    assert mint_rev is True
    assert freeze_rev is True
    assert len(fails) == 0


# ---------------------------------------------------------------------------
# Gate 2: Holder Concentration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_holder_concentration_exceeded():
    # Top holder controls 80% of supply
    mock_holders = [
        {"address": "Holder11111111111111111111111111111111111111", "uiAmount": 800000.0},
        {"address": "Holder22222222222222222222222222222222222222", "uiAmount": 50000.0},
    ]
    total_supply = 1000000.0

    is_ok, top10_pct, fails = await check_holder_concentration(
        mint_address=MINT_SAMPLE,
        rpc_url="http://mock",
        max_top10_pct=50.0,
        mock_holders_data=mock_holders,
        total_supply=total_supply,
    )
    assert is_ok is False
    assert top10_pct == 85.0
    assert len(fails) == 1
    assert "exceeds limit" in fails[0]


@pytest.mark.asyncio
async def test_holder_concentration_safe():
    # Top holders total 25% of supply
    mock_holders = [
        {"address": f"Holder{i}111111111111111111111111111111111111", "uiAmount": 25000.0}
        for i in range(10)
    ]
    total_supply = 1000000.0

    is_ok, top10_pct, fails = await check_holder_concentration(
        mint_address=MINT_SAMPLE,
        rpc_url="http://mock",
        max_top10_pct=50.0,
        mock_holders_data=mock_holders,
        total_supply=total_supply,
    )
    assert is_ok is True
    assert top10_pct == 25.0
    assert len(fails) == 0


# ---------------------------------------------------------------------------
# Gate 3: Liquidity & LP Status
# ---------------------------------------------------------------------------
def test_liquidity_insufficient():
    passed, lp_status, fails = check_liquidity_and_lp(
        liquidity_usd=4000.0,
        min_liquidity_usd=10000.0,
        lp_burned_pct=100.0,
    )
    assert passed is False
    assert any("Liquidity insufficient" in f for f in fails)


def test_lp_unlocked_fails():
    passed, lp_status, fails = check_liquidity_and_lp(
        liquidity_usd=25000.0,
        min_liquidity_usd=10000.0,
        lp_must_be_burned_or_locked=True,
        lp_burned_pct=0.0,
        lp_locked_pct=0.0,
    )
    assert passed is False
    assert lp_status == LPStatus.UNLOCKED
    assert any("LP is not burned or locked" in f for f in fails)


def test_lp_burned_passes():
    passed, lp_status, fails = check_liquidity_and_lp(
        liquidity_usd=25000.0,
        min_liquidity_usd=10000.0,
        lp_must_be_burned_or_locked=True,
        lp_burned_pct=100.0,
    )
    assert passed is True
    assert lp_status == LPStatus.BURNED
    assert len(fails) == 0


# ---------------------------------------------------------------------------
# Gate 4: Honeypot Simulation & Taxes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_honeypot_sell_blocked():
    mock_sim = {"can_sell": False, "error": "Transaction simulation failed: Custom error 0x1"}
    is_safe, sell_tax, transfer_fee, fails = await check_honeypot_and_fees(
        mint_address=MINT_SAMPLE,
        mock_simulation=mock_sim,
    )
    assert is_safe is False
    assert any("Honeypot detected" in f for f in fails)


@pytest.mark.asyncio
async def test_honeypot_excessive_tax():
    mock_sim = {"can_sell": True, "sell_tax_pct": 20.0, "transfer_fee_pct": 0.0}
    is_safe, sell_tax, transfer_fee, fails = await check_honeypot_and_fees(
        mint_address=MINT_SAMPLE,
        max_sell_tax_pct=5.0,
        mock_simulation=mock_sim,
    )
    assert is_safe is False
    assert sell_tax == 20.0
    assert any("Excessive sell tax" in f for f in fails)


# ---------------------------------------------------------------------------
# Gate 5: RugCheck API Signal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rugcheck_danger_flag():
    mock_rc = {
        "score": 300,
        "risks": [
            {"name": "Mutable Metadata", "level": "warn", "description": "Owner can edit metadata"},
            {"name": "Low Liquidity", "level": "danger", "description": "Insufficient liquidity depth"}
        ]
    }
    passed, score, risks, fails = await check_rugcheck(
        mint_address=MINT_SAMPLE,
        mock_response=mock_rc,
    )
    assert passed is False
    assert any("RugCheck danger risk" in f for f in fails)


# ---------------------------------------------------------------------------
# Gate 6: Deployer History
# ---------------------------------------------------------------------------
def test_deployer_too_many_rugs():
    history = {"total_deploys": 5, "rugged_deploys": 4}
    is_ok, notes, fails = check_deployer_history(
        deployer_address="Deployer111111111111111111111111111111111",
        max_rugged_deploys=1,
        deployer_history=history,
    )
    assert is_ok is False
    assert any("rugged deploys" in f for f in fails)


# ---------------------------------------------------------------------------
# Integrated SecurityScreener Pipeline Test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_screener_pipeline_clean_token_passes():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        screening=ScreeningConfig(
            top10_holder_max_pct=50.0,
            min_liquidity_usd=10000.0,
            lp_must_be_burned_or_locked=True,
            max_sell_tax_pct=5.0,
        )
    )

    screener = SecurityScreener(config=config, database=db)

    # Candidate token
    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="CLEAN",
        source=TokenSource.DEXSCREENER,
        dex=DexType.RAYDIUM,
        initial_liquidity_usd=50000.0,
        initial_volume_24h=120000.0,
    )
    token_id = await db.insert_token(token)

    # Injected clean mock responses
    mock_auth = {"mintAuthority": None, "freezeAuthority": None}
    mock_holders = [
        {"address": f"Holder{i}111111111111111111111111111111111111", "uiAmount": 20000.0}
        for i in range(10)
    ]
    mock_sim = {"can_sell": True, "sell_tax_pct": 0.0, "transfer_fee_pct": 0.0}
    mock_rc = {"score": 85, "risks": []}
    mock_dep = {"total_deploys": 1, "rugged_deploys": 0}

    # Execute screening
    result = await screener.screen_token(
        token=token,
        token_id=token_id,
        mock_authorities=mock_auth,
        mock_holders=mock_holders,
        mock_total_supply=1000000.0,
        mock_lp_burned_pct=100.0,
        mock_simulation=mock_sim,
        mock_rugcheck=mock_rc,
        mock_deployer=mock_dep,
    )

    # Verify pass
    assert result.passed is True
    assert len(result.fail_reasons) == 0
    assert result.mint_authority_revoked is True
    assert result.freeze_authority_revoked is True
    assert result.holder_concentration_ok is True
    assert result.honeypot_safe is True

    # Verify saved in SQLite database
    cursor = await db.conn.execute("SELECT * FROM screening_results WHERE token_id = ?", (token_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert row["passed"] == 1

    await screener.close()
    await db.close()


@pytest.mark.asyncio
async def test_screener_pipeline_gate_keras_skips_and_logs():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig()
    screener = SecurityScreener(config=config, database=db)

    # Token with unrevoked mint authority
    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="RUGGY",
        source=TokenSource.DEXSCREENER,
        dex=DexType.RAYDIUM,
        initial_liquidity_usd=20000.0,
    )
    token_id = await db.insert_token(token)

    # Active mint authority
    bad_auth = {"mintAuthority": "Attacker1111111111111111111111111111111", "freezeAuthority": None}

    result = await screener.screen_token(
        token=token,
        token_id=token_id,
        mock_authorities=bad_auth,
        mock_holders=[{"address": "H1", "uiAmount": 100.0}],
        mock_lp_burned_pct=100.0,
        mock_simulation={"can_sell": True},
        mock_rugcheck={"score": 80, "risks": []},
    )

    # Gate keras: must fail
    assert result.passed is False
    assert any("Mint authority active" in r for r in result.fail_reasons)

    # Verify recorded in decisions_log
    cursor = await db.conn.execute(
        "SELECT * FROM decisions_log WHERE decision_type = ?",
        (DecisionType.SKIP_SCREENING.value,)
    )
    log_row = await cursor.fetchone()
    assert log_row is not None
    assert log_row["token_id"] == token_id
    assert "Mint authority active" in log_row["reason"]

    await screener.close()
    await db.close()
