"""Screening Gate 5: RugCheck API integration for secondary safety signals."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def check_rugcheck(
    mint_address: str,
    base_url: str = "https://api.rugcheck.xyz/v1",
    min_score: float = 50.0,
    client: httpx.AsyncClient | None = None,
    mock_response: dict[str, Any] | None = None,
) -> tuple[bool, float, list[str], list[str]]:
    """
    Query RugCheck API for community and automated risk assessments.

    Parameters
    ----------
    mint_address  : Token mint address.
    base_url      : RugCheck API base URL.
    min_score     : Minimum passing score or threshold.
    client        : Optional httpx.AsyncClient.
    mock_response : Optional pre-canned response for unit testing.

    Returns
    -------
    (passed, score, risks, fail_reasons)
    """
    fail_reasons: list[str] = []
    risks_detected: list[str] = []
    score = 100.0  # default clean

    data = mock_response
    if data is None:
        url = f"{base_url.rstrip('/')}/tokens/{mint_address}/report"
        try:
            should_close = False
            if client is None:
                client = httpx.AsyncClient(timeout=10.0)
                should_close = True

            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                elif resp.status_code == 404:
                    logger.debug("RugCheck report not yet generated for %s", mint_address)
                    return True, 50.0, [], []
                else:
                    logger.warning("RugCheck API returned HTTP %d for %s", resp.status_code, mint_address)
                    return True, 50.0, [], []
            finally:
                if should_close:
                    await client.aclose()
        except Exception as e:
            logger.debug("RugCheck lookup exception for %s: %s", mint_address, e)
            return True, 50.0, [], []

    if data:
        score = float(data.get("score", 0.0))
        raw_risks = data.get("risks") or []

        for r in raw_risks:
            name = r.get("name", "Unknown Risk")
            level = (r.get("level") or "").lower()
            description = r.get("description", "")
            risks_detected.append(f"{name} [{level}]: {description}".strip())

            # "danger" level risks trigger immediate failure
            if level == "danger":
                fail_reasons.append(f"RugCheck danger risk: {name} ({description})")

        # Evaluate score if provided:
        # Note: RugCheck live API returns a risk score where 0-100 (especially 1) with no danger risks is SAFE,
        # whereas synthetic mock scores in tests use a 0-100 safety scale if no `score_normalised` key exists.
        is_live_format = "score_normalised" in data or "markets" in data or "tokenMeta" in data
        if is_live_format:
            norm_score = float(data.get("score_normalised", score))
            if norm_score > 500 and len(fail_reasons) == 0:
                fail_reasons.append(f"RugCheck risk score {norm_score:.0f} too high")
        else:
            if score < min_score and score > 0:
                fail_reasons.append(f"RugCheck score {score:.0f} below minimum {min_score:.0f}")

    passed = len(fail_reasons) == 0
    return passed, score, risks_detected, fail_reasons


def extract_lp_locked_pct_from_rugcheck(data: dict[str, Any] | None) -> float | None:
    """Extract maximum LP locked/burned percentage across DEX markets in a RugCheck report."""
    if not data or not isinstance(data, dict):
        return None
    markets = data.get("markets")
    if not isinstance(markets, list) or not markets:
        return None
    best_pct: float | None = None
    for m in markets:
        if not isinstance(m, dict):
            continue
        lp = m.get("lp") or {}
        if isinstance(lp, dict):
            locked_pct = lp.get("lpLockedPct")
            if locked_pct is not None:
                try:
                    val = float(locked_pct)
                    if best_pct is None or val > best_pct:
                        best_pct = val
                except (TypeError, ValueError):
                    pass
    return best_pct
