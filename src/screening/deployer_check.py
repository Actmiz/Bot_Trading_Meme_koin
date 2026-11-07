"""Screening Gate 6: Deployer History Check.

Examines the deployer wallet address for previous malicious launches or rugs.
Skips tokens launched by deployers with past rug pull history.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def check_deployer_history(
    deployer_address: str | None,
    max_rugged_deploys: int = 1,
    deployer_history: dict[str, Any] | None = None,
) -> tuple[bool, str | None, list[str]]:
    """
    Check if the deployer wallet has a known history of rug pulls.

    Parameters
    ----------
    deployer_address   : Public key of the token creator/deployer.
    max_rugged_deploys : Max acceptable previous rugged tokens (default 1).
    deployer_history   : Optional historical summary dict (total_deploys, rugged_deploys).

    Returns
    -------
    (is_ok, notes, fail_reasons)
    """
    fail_reasons: list[str] = []

    if not deployer_address:
        # Deployer address unknown / not indexed yet; pass with note
        return True, "Deployer address unknown", []

    if deployer_history:
        total = deployer_history.get("total_deploys", 0)
        rugged = deployer_history.get("rugged_deploys", 0)
        notes = f"Deployer {deployer_address[:6]}... launched {total} tokens, {rugged} rugged."

        if rugged > max_rugged_deploys:
            fail_reasons.append(
                f"Deployer has {rugged} rugged deploys (exceeds limit of {max_rugged_deploys})"
            )
            return False, notes, fail_reasons

        return True, notes, []

    return True, "No adverse deployer history found", []
