"""
Performance-gated aggression.

Aggression is earned by the ledger, not declared by mood.  The tier rises
automatically as settled bets accumulate and realized ROI proves the model,
and falls automatically if the edge degrades.  This is the professional
version of aggressive: maximum size on a proven edge, minimum size while
it is still a hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AggressionTier:
    """Stake schedule unlocked by demonstrated performance."""

    level: int
    name: str
    min_settled: int
    min_roi: float
    single_stake_units: float
    sure_stake_units: float
    acca_stake_units: float
    note: str


TIERS: tuple[AggressionTier, ...] = (
    AggressionTier(
        0, "CALIBRATION", 0, -1.0, 1.0, 1.0, 0.5,
        "Flat 1u until the ledger speaks. No exceptions.",
    ),
    AggressionTier(
        1, "PROVEN-START", 25, 0.0, 1.5, 1.5, 0.75,
        "Edge survived 25 settled bets at non-negative ROI — stakes up 50%.",
    ),
    AggressionTier(
        2, "PROVEN", 100, 0.0, 2.0, 2.0, 1.0,
        "Edge survived 100 settled bets — full policy size.",
    ),
    AggressionTier(
        3, "FULL-AGGRESSION", 150, 0.05, 3.0, 3.0, 1.5,
        "Edge above +5% over 150 settled bets — maximum size.",
    ),
)


def current_tier(metrics: dict) -> AggressionTier:
    """Highest tier whose settled-count and ROI requirements are both met."""
    settled = int(metrics.get("settled", 0))
    roi = float(metrics.get("roi", 0.0))
    tier = TIERS[0]
    for t in TIERS:
        if settled >= t.min_settled and roi >= t.min_roi:
            tier = t
    return tier