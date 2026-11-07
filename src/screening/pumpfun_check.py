"""Screening Gate 7: Pump.fun-specific safety (bonding-curve phase, dev bundles, insider dumps).

Pump.fun tokens are contract-safe by design (mint/freeze revoked, 0% tax, LP burned on
graduation), but most die within minutes because of dev bundle wallets and insider dumps.
This gate only allows Pump.fun tokens that are:
  * already graduated to PumpSwap / Raydium, OR
  * still on the bonding curve but at "King of the Hill" stage (high market cap),
and applies a stricter holder-concentration ceiling plus bundle / insider / dump detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.enums import DexType
from src.core.models import TokenSnapshot

# RugCheck risk names (lower-cased substrings) that indicate coordinated dev/insider supply.
BUNDLE_RISK_KEYWORDS: tuple[str, ...] = (
    "bundle",
    "insider",
    "holder correlation",
    "single holder ownership",
    "creator history",
    "high ownership",
)

PHASE_BONDING_CURVE = "bonding_curve"
PHASE_GRADUATED = "graduated"
PHASE_NOT_PUMPFUN = "not_pumpfun"


@dataclass
class PumpFunCheckResult:
    """Outcome of the Pump.fun-specific gate."""

    is_pumpfun: bool
    phase: str
    passed: bool
    fail_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pair_meta(token: TokenSnapshot) -> dict[str, Any]:
    meta = token.metadata_json or {}
    pair = meta.get("pair") if isinstance(meta, dict) else None
    return pair if isinstance(pair, dict) else {}


def detect_pumpfun_phase(token: TokenSnapshot) -> tuple[bool, str]:
    """
    Determine whether the token originates from Pump.fun and which phase it is in.

    Returns (is_pumpfun, phase) where phase is one of
    `bonding_curve`, `graduated`, or `not_pumpfun`.
    """
    pair = _pair_meta(token)
    dex_id = str(pair.get("dexId") or "").lower()
    mint = token.mint_address or ""

    is_pumpfun = (
        mint.lower().endswith("pump")
        or token.dex == DexType.PUMPFUN
        or dex_id in ("pumpfun", "pumpswap")
    )
    if not is_pumpfun:
        return False, PHASE_NOT_PUMPFUN

    # DexScreener reports the internal Pump.fun curve as dexId "pumpfun";
    # graduated tokens trade on "pumpswap" (or "raydium" for older migrations).
    if dex_id == "pumpfun":
        return True, PHASE_BONDING_CURVE
    if dex_id in ("pumpswap", "raydium", "meteora", "orca"):
        return True, PHASE_GRADUATED
    # Unknown pair source: treat conservatively as still on the curve.
    return True, PHASE_BONDING_CURVE


def check_pumpfun_safety(
    token: TokenSnapshot,
    top10_holder_pct: float | None,
    rugcheck_risks: list[str] | None,
    top10_max_pct: float = 35.0,
    min_curve_mcap_usd: float = 35000.0,
    min_age_sec: int = 120,
    block_bundle_risks: bool = True,
    max_m5_dump_pct: float = -25.0,
) -> PumpFunCheckResult:
    """
    Apply Pump.fun-specific safety rules.

    Parameters
    ----------
    token              : Candidate token snapshot (with DexScreener pair metadata if available).
    top10_holder_pct   : Top-10 holder % (excluding LP / curve) from Gate 2, if known.
    rugcheck_risks     : Risk strings from Gate 5 (RugCheck), if available.
    top10_max_pct      : Stricter top-10 ceiling for Pump.fun tokens (default 35%).
    min_curve_mcap_usd : Minimum market cap for tokens still on the bonding curve ("King of the Hill").
    min_age_sec        : Minimum pair age to avoid the first-minutes dev/sniper bundle window.
    block_bundle_risks : Reject when RugCheck flags bundle / insider / correlated holders.
    max_m5_dump_pct    : Reject when the 5-minute price change is below this (active dev dump).
    """
    is_pumpfun, phase = detect_pumpfun_phase(token)
    if not is_pumpfun:
        return PumpFunCheckResult(is_pumpfun=False, phase=phase, passed=True)

    fails: list[str] = []
    notes: list[str] = [f"phase={phase}"]
    pair = _pair_meta(token)

    # 1. Phase gate: early bonding curve is where >95% of Pump.fun rugs/dev dumps happen.
    if phase == PHASE_BONDING_CURVE:
        mcap = float(token.initial_mcap_usd or pair.get("fdv") or pair.get("marketCap") or 0.0)
        if mcap < min_curve_mcap_usd:
            fails.append(
                f"PumpFun early bonding curve: mcap ${mcap:,.0f} < King-of-the-Hill min ${min_curve_mcap_usd:,.0f}"
            )
        else:
            notes.append(f"king_of_the_hill mcap=${mcap:,.0f}")

    # 2. Minimum age: skip the first minutes where bundle wallets dump on retail.
    if token.token_age_seconds is not None and token.token_age_seconds < min_age_sec:
        fails.append(
            f"PumpFun token too young: {token.token_age_seconds}s < {min_age_sec}s (bundle/sniper window)"
        )

    # 3. Stricter holder concentration (dev bundles split supply across many wallets).
    if top10_holder_pct is not None and top10_holder_pct > top10_max_pct:
        fails.append(
            f"PumpFun top10 holders {top10_holder_pct:.1f}% > strict max {top10_max_pct:.1f}%"
        )

    # 4. Bundle / insider detection from RugCheck risk names.
    if block_bundle_risks and rugcheck_risks:
        for risk in rugcheck_risks:
            low = risk.lower()
            if any(k in low for k in BUNDLE_RISK_KEYWORDS):
                fails.append(f"PumpFun bundle/insider risk: {risk.split(':')[0].strip()}")
                break

    # 5. Active dev dump: sharp 5m drop with sells dominating buys.
    price_change = pair.get("priceChange") or {}
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    try:
        m5_pct = float(price_change.get("m5")) if price_change.get("m5") is not None else None
    except (TypeError, ValueError):
        m5_pct = None
    buys_m5 = int(txns_m5.get("buys") or 0)
    sells_m5 = int(txns_m5.get("sells") or 0)
    if m5_pct is not None and m5_pct <= max_m5_dump_pct and sells_m5 > buys_m5:
        fails.append(
            f"PumpFun active dump: 5m price {m5_pct:+.1f}% with sells {sells_m5} > buys {buys_m5}"
        )

    return PumpFunCheckResult(
        is_pumpfun=True,
        phase=phase,
        passed=len(fails) == 0,
        fail_reasons=fails,
        notes=notes,
    )

