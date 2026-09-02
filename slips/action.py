"""
ACTION tier — daily picks when no edge is detected.

Honest contract: these picks carry NO measured edge.  They are the
candidates closest to breakeven available right now (highest edge, even if
negative), inside a sane odds band, with enough books behind the price for
the consensus to mean something.  Fixed stake, hard cap per session,
slip_type "ACTION" so the ledger measures their ROI separately.

After ~50 settled ACTION bets, the data decides whether this tier stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config.settings import ACTION_SETTINGS
from odds.comparator import BetOpportunity
from slips.generator import Slip, SlipLeg


@dataclass(frozen=True)
class ActionConfig:
    """Knobs for ACTION mode (mirrors config.ActionSettings)."""

    max_picks: int
    stake_units: float
    min_odds: float
    max_odds: float
    min_prob: float
    min_books: int

    @classmethod
    def from_settings(cls) -> "ActionConfig":
        s = ACTION_SETTINGS
        return cls(
            max_picks=s.max_picks,
            stake_units=s.stake_units,
            min_odds=s.min_odds,
            max_odds=s.max_odds,
            min_prob=s.min_prob,
            min_books=s.min_books,
        )


def build_action_picks(
    opportunities: Sequence[BetOpportunity],
    exclude_match_ids: set[str] | frozenset[str] = frozenset(),
    config: ActionConfig | None = None,
) -> list[Slip]:
    """Build ACTION slips: closest-to-value candidates, one per match.

    Ranking is by edge descending (least negative first = closest to
    breakeven), then EV.  Odds band and book-coverage filters keep the
    picks inside a reasonable range.
    """
    cfg = config if config is not None else ActionConfig.from_settings()

    pool = [
        opp
        for opp in opportunities
        if cfg.min_odds <= opp.decimal_odds <= cfg.max_odds
        and opp.selection.model_probability >= cfg.min_prob
        and len(opp.quotes_by_book) >= cfg.min_books
        and opp.selection.match_id not in exclude_match_ids
    ]
    pool.sort(key=lambda o: (o.edge, o.ev_per_unit), reverse=True)

    slips: list[Slip] = []
    used: set[str] = set()
    for opp in pool:
        if len(slips) >= cfg.max_picks:
            break
        if opp.selection.match_id in used:
            continue
        slips.append(
            Slip(
                slip_type="ACTION",
                legs=[SlipLeg.from_opportunity(opp)],
                stake_units=cfg.stake_units,
            )
        )
        used.add(opp.selection.match_id)
    return slips