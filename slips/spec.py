"""
SPEC tier — controlled consensus-vs-outlier singles.

What these are:
    A selection where the cross-book no-vig CONSENSUS probability is above
    what the best-priced book charges.  The best price is an outlier; either
    that book knows something (team news) or it is simply mispriced.  These
    are the classic cross-platform inefficiencies — and also the noisiest,
    so they get special treatment:

        * fixed quarter-unit stake (0.25u) — never scaled up by tiers
        * minimum 4 independent books behind the pick (kills 2-book artifacts)
        * odds capped at 13.0, probability floored at 8%
        * maximum 2 per session
        * logged as slip_type "SPEC" so the ledger measures their ROI
          separately from everything else

    The experiment contract: after ~50 settled SPEC bets, their ROI decides
    whether this tier stays, shrinks, or gets deleted.  Data, not vibes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from odds.comparator import BetOpportunity
from slips.generator import Slip, SlipLeg


@dataclass(frozen=True)
class SpecConfig:
    """Knobs for the SPEC tier.  Deliberately small-stakes by design."""

    min_prob: float = 0.08       # below this it is pure noise
    max_odds: float = 13.0       # longshot cap
    min_books: int = 4           # independent books behind the pick
    min_edge: float = 0.02       # consensus must beat the price by >= 2pp
    stake_units: float = 0.25    # fixed quarter-unit stake
    max_per_session: int = 2

    def __post_init__(self) -> None:
        if not 0.0 < self.min_prob < 1.0:
            raise ValueError("min_prob must lie in (0, 1).")
        if self.max_odds <= 1.0:
            raise ValueError("max_odds must be greater than 1.0.")
        if self.min_books < 1:
            raise ValueError("min_books must be at least 1.")
        if self.min_edge < 0.0:
            raise ValueError("min_edge must be non-negative.")
        if self.stake_units <= 0.0:
            raise ValueError("stake_units must be positive.")
        if self.max_per_session < 0:
            raise ValueError("max_per_session must be non-negative.")


SPEC_CONFIG = SpecConfig()


def build_spec_singles(
    opportunities: Sequence[BetOpportunity],
    exclude_match_ids: set[str] | frozenset[str] = frozenset(),
    config: SpecConfig = SPEC_CONFIG,
) -> list[Slip]:
    """Build SPEC singles from consensus-outlier opportunities.

    Sorted by EV, one pick per match, capped per session.  Matches already
    used by SURESLIP/singles/accas are excluded.
    """
    pool = [
        opp
        for opp in opportunities
        if opp.selection.model_probability >= config.min_prob
        and opp.decimal_odds <= config.max_odds
        and len(opp.quotes_by_book) >= config.min_books
        and opp.edge >= config.min_edge
        and opp.selection.match_id not in exclude_match_ids
    ]
    pool.sort(key=lambda opp: opp.ev_per_unit, reverse=True)

    slips: list[Slip] = []
    used: set[str] = set()
    for opp in pool:
        if len(slips) >= config.max_per_session:
            break
        if opp.selection.match_id in used:
            continue
        slips.append(
            Slip(
                slip_type="SPEC",
                legs=[SlipLeg.from_opportunity(opp)],
                stake_units=config.stake_units,
            )
        )
        used.add(opp.selection.match_id)
    return slips