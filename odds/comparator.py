"""
Expected-value comparison between model probabilities and platform odds.

    implied probability    q      = 1 / o
    expected value (1u)    EV     = p * o - 1
    edge                   E      = p - q
    fair odds              o_fair = 1 / p
    full Kelly fraction    f*     = (p * o - 1) / (o - 1)

Every opportunity carries the full quote map (book -> odds) so the slip
builder can choose the single best platform for a whole slip.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

MIN_DECIMAL_ODDS = 1.01


@dataclass(frozen=True)
class OddsQuote:
    """One platform's decimal price for one selection."""

    book: str
    decimal_odds: float

    @property
    def implied_probability(self) -> float:
        return 1.0 / self.decimal_odds

    def __post_init__(self) -> None:
        if self.decimal_odds < MIN_DECIMAL_ODDS:
            raise ValueError(f"Decimal odds must be >= {MIN_DECIMAL_ODDS}.")
        if not self.book:
            raise ValueError("Book name must be non-empty.")


@dataclass(frozen=True)
class Selection:
    """A modelled betting selection awaiting a market price."""

    match_id: str
    match_label: str
    league: str
    market: str
    selection: str
    model_probability: float

    def __post_init__(self) -> None:
        if not 0.0 < self.model_probability < 1.0:
            raise ValueError("model_probability must lie strictly inside (0, 1).")


@dataclass(frozen=True)
class BetOpportunity:
    """A fully priced selection: model probability vs the best available odds."""

    selection: Selection
    book: str
    decimal_odds: float
    implied_probability: float
    edge: float
    ev_per_unit: float
    fair_odds: float
    kelly_full: float
    quotes_by_book: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return (
            f"{self.selection.match_label} | "
            f"{self.selection.market} -> {self.selection.selection}"
        )

    @property
    def is_positive_ev(self) -> bool:
        return self.ev_per_unit > 0.0 and self.edge > 0.0


class OddsComparator:
    """Prices model selections, computes EV, and flags positive-EV bets."""

    def __init__(
        self,
        min_edge: float = 0.03,
        min_ev_per_unit: float = 0.03,
        min_model_prob: float = 0.0,
        max_odds: float = 15.0,
    ) -> None:
        if not 0.0 <= min_edge < 1.0:
            raise ValueError("min_edge must be in [0, 1).")
        if min_ev_per_unit < 0.0:
            raise ValueError("min_ev_per_unit must be non-negative.")
        if not 0.0 <= min_model_prob < 1.0:
            raise ValueError("min_model_prob must be in [0, 1).")
        if max_odds <= 1.0:
            raise ValueError("max_odds must be greater than 1.0.")

        self.min_edge = min_edge
        self.min_ev_per_unit = min_ev_per_unit
        self.min_model_prob = min_model_prob
        self.max_odds = max_odds

    def evaluate(
        self, selection: Selection, quotes: Sequence[OddsQuote]
    ) -> BetOpportunity:
        """Price at the best quote (line shopping) and compute EV."""
        if not quotes:
            raise ValueError("At least one quote is required to price a selection.")

        best = max(quotes, key=lambda q: q.decimal_odds)
        p = selection.model_probability
        o = best.decimal_odds

        return BetOpportunity(
            selection=selection,
            book=best.book,
            decimal_odds=o,
            implied_probability=1.0 / o,
            edge=p - 1.0 / o,
            ev_per_unit=p * o - 1.0,
            fair_odds=1.0 / p,
            kelly_full=(p * o - 1.0) / (o - 1.0) if o > 1.0 else 0.0,
            quotes_by_book={q.book: q.decimal_odds for q in quotes},
        )

    def is_value(self, opportunity: BetOpportunity) -> bool:
        return (
            opportunity.is_positive_ev
            and opportunity.edge >= self.min_edge
            and opportunity.ev_per_unit >= self.min_ev_per_unit
            and opportunity.selection.model_probability >= self.min_model_prob
            and opportunity.decimal_odds <= self.max_odds
        )

    def rank(self, opportunities: Iterable[BetOpportunity]) -> list[BetOpportunity]:
        return sorted(opportunities, key=lambda opp: opp.ev_per_unit, reverse=True)

    def find_value_bets(
        self,
        candidates: Iterable[tuple[Selection, Sequence[OddsQuote]]],
    ) -> list[BetOpportunity]:
        opportunities = [
            self.evaluate(selection, quotes) for selection, quotes in candidates
        ]
        return self.rank([opp for opp in opportunities if self.is_value(opp)])

    @staticmethod
    def bookmaker_margin(odds: Sequence[float]) -> float:
        """Overround: sum(1/odds) - 1 across exclusive outcomes."""
        if not odds or any(o < MIN_DECIMAL_ODDS for o in odds):
            raise ValueError("Provide valid decimal odds for each market outcome.")
        return sum(1.0 / o for o in odds) - 1.0

    @staticmethod
    def no_vig_probabilities(odds: Sequence[float]) -> list[float]:
        if not odds or any(o < MIN_DECIMAL_ODDS for o in odds):
            raise ValueError("Provide valid decimal odds for each market outcome.")
        implied = [1.0 / o for o in odds]
        total = sum(implied)
        return [p / total for p in implied]
