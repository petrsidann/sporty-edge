"""
Bet slip construction.

Tiers, in build order:

    SURESLIP : strictest tier. Every pick >= min_leg_prob (default 85%),
               one pick per match, combined win probability gated
               (default >= 50%). True combined probability always printed.
    SINGLE   : one high-EV pick per match.
    ACCA     : 2-4 legs, every leg >= 55% model probability, capped odds.

Legs within one slip always come from different matches, so multiplying
probabilities is reasonable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import RISK_SETTINGS, SURESLIP_SETTINGS
from odds.comparator import BetOpportunity


def _short_id() -> str:
    """Internal reference id (NOT a platform booking code)."""
    return uuid.uuid4().hex[:8].upper()


@dataclass(frozen=True)
class SlipLeg:
    """One leg of a slip — a frozen snapshot of a priced opportunity."""

    match_id: str
    match_label: str
    league: str
    market: str
    selection: str
    book: str
    decimal_odds: float
    model_prob: float
    edge: float
    ev_per_unit: float
    quotes_by_book: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_opportunity(cls, opportunity: BetOpportunity) -> "SlipLeg":
        return cls(
            match_id=opportunity.selection.match_id,
            match_label=opportunity.selection.match_label,
            league=opportunity.selection.league,
            market=opportunity.selection.market,
            selection=opportunity.selection.selection,
            book=opportunity.book,
            decimal_odds=opportunity.decimal_odds,
            model_prob=opportunity.selection.model_probability,
            edge=opportunity.edge,
            ev_per_unit=opportunity.ev_per_unit,
            quotes_by_book=dict(opportunity.quotes_by_book),
        )


@dataclass
class Slip:
    """A placeable bet slip (SURESLIP, SINGLE, or ACCA)."""

    slip_type: str
    legs: list[SlipLeg]
    stake_units: float
    slip_id: str = field(default_factory=_short_id)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def combined_odds(self) -> float:
        odds = 1.0
        for leg in self.legs:
            odds *= leg.decimal_odds
        return odds

    @property
    def combined_prob(self) -> float:
        prob = 1.0
        for leg in self.legs:
            prob *= leg.model_prob
        return prob

    @property
    def ev_per_unit(self) -> float:
        return self.combined_prob * self.combined_odds - 1.0

    def summary_line(self) -> str:
        return (
            f"{self.slip_type:<8} {self.slip_id}  odds {self.combined_odds:6.2f}  "
            f"prob {self.combined_prob * 100:5.1f}%  "
            f"EV/u {self.ev_per_unit * 100:+6.1f}%  "
            f"stake {self.stake_units:.2f}u"
        )

    def render(self) -> str:
        width = 68
        bar = "=" * width
        sub = "-" * width

        lines = [bar]
        lines.append(
            f" SLIP {self.slip_id}  |  {self.slip_type}  |  "
            f"{self.n_legs} pick(s)  |  {self.created_at}"
        )
        lines.append(f" (Internal ref only — the platform mints the real code.)")
        lines.append(sub)
        for idx, leg in enumerate(self.legs, start=1):
            lines.append(f" Pick {idx}: {leg.match_label}  [{leg.league}]")
            lines.append(f"   Pick : {leg.market} -> {leg.selection}")
            lines.append(f"   Best : {leg.book} @ {leg.decimal_odds:.2f}")
            lines.append(
                f"   Model: {leg.model_prob * 100:5.1f}% | "
                f"Fair {1.0 / leg.model_prob:.2f} | "
                f"Edge {leg.edge * 100:+.1f}% | EV/u {leg.ev_per_unit * 100:+.1f}%"
            )
        lines.append(sub)
        lines.append(f" Combined odds : {self.combined_odds:.2f}")
        lines.append(
            f" True win prob : {self.combined_prob * 100:.1f}%"
            f"  ->  expected to land ~{self.combined_prob * 10:.0f} of 10"
        )
        lines.append(f" EV per unit   : {self.ev_per_unit * 100:+.1f}%")
        lines.append(f" Stake         : {self.stake_units:.2f} units")
        lines.append(bar)
        return "\n".join(lines)


@dataclass(frozen=True)
class SlipGeneratorConfig:
    """Knobs for singles and standard accas."""

    max_singles: int = 3
    single_stake_units: float = 1.0
    min_single_prob: float = 0.40
    max_single_odds: float = 4.00

    max_accas: int = 2
    acca_stake_units: float = 0.5
    min_acca_leg_prob: float = 0.55
    max_acca_leg_odds: float = 2.50
    max_acca_legs: int = 4
    min_acca_combined_prob: float = 0.28
    max_acca_combined_odds: float = 9.00

    def __post_init__(self) -> None:
        if self.max_acca_legs > 4:
            raise ValueError("max_acca_legs is capped at 4 by design.")
        if not 0.0 < self.min_single_prob < 1.0 or not 0.0 < self.min_acca_leg_prob < 1.0:
            raise ValueError("Probability thresholds must lie in (0, 1).")
        if not 0.0 < self.min_acca_combined_prob < 1.0:
            raise ValueError("min_acca_combined_prob must lie in (0, 1).")
        if self.max_single_odds <= 1.0 or self.max_acca_leg_odds <= 1.0:
            raise ValueError("Odds caps must be greater than 1.0.")
        if self.max_acca_combined_odds <= 1.0:
            raise ValueError("max_acca_combined_odds must be greater than 1.0.")
        if self.single_stake_units <= 0.0 or self.acca_stake_units <= 0.0:
            raise ValueError("Stakes must be positive.")
        if self.max_singles < 0 or self.max_accas < 0:
            raise ValueError("Slip counts must be non-negative.")

    @classmethod
    def from_settings(cls) -> "SlipGeneratorConfig":
        r = RISK_SETTINGS
        return cls(
            max_singles=r.max_singles,
            max_accas=r.max_accas,
            single_stake_units=r.single_stake_units,
            acca_stake_units=r.acca_stake_units,
            min_single_prob=r.min_model_prob_single,
            max_single_odds=r.max_single_odds,
            min_acca_leg_prob=r.min_acca_leg_prob,
            max_acca_leg_odds=r.max_acca_leg_odds,
            max_acca_legs=r.max_acca_legs,
            min_acca_combined_prob=r.min_acca_combined_prob,
            max_acca_combined_odds=r.max_acca_combined_odds,
        )


@dataclass(frozen=True)
class SureSlipConfig:
    """Knobs for SURESLIP construction."""

    min_legs: int
    max_legs: int
    min_leg_prob: float
    max_leg_odds: float
    min_combined_prob: float
    min_combined_odds: float
    max_combined_odds: float
    stake_units: float
    max_sure_slips: int

    def __post_init__(self) -> None:
        if not 2 <= self.min_legs <= self.max_legs <= 10:
            raise ValueError("Require 2 <= min_legs <= max_legs <= 10.")
        if not 0.0 < self.min_leg_prob < 1.0 or not 0.0 < self.min_combined_prob < 1.0:
            raise ValueError("Probability floors must lie in (0, 1).")
        if self.max_leg_odds <= 1.0 or self.max_combined_odds <= 1.0:
            raise ValueError("Odds caps must be greater than 1.0.")
        if self.min_combined_odds < 1.0 or self.min_combined_odds > self.max_combined_odds:
            raise ValueError("min_combined_odds must be >= 1.0 and <= max.")
        if self.stake_units <= 0.0 or self.max_sure_slips < 0:
            raise ValueError("Invalid stake or slip count.")

    @classmethod
    def from_settings(cls) -> "SureSlipConfig":
        s = SURESLIP_SETTINGS
        return cls(
            min_legs=s.min_legs,
            max_legs=s.max_legs,
            min_leg_prob=s.min_leg_prob,
            max_leg_odds=s.max_leg_odds,
            min_combined_prob=s.min_combined_prob,
            min_combined_odds=s.min_combined_odds,
            max_combined_odds=s.max_combined_odds,
            stake_units=s.stake_units,
            max_sure_slips=s.max_sure_slips,
        )


class SlipGenerator:
    """Builds SURESLIPs, singles, and short accas from value bets."""

    def __init__(
        self,
        config: SlipGeneratorConfig | None = None,
        sure_config: SureSlipConfig | None = None,
    ) -> None:
        self.config = config if config is not None else SlipGeneratorConfig.from_settings()
        self.sure_config = (
            sure_config if sure_config is not None else SureSlipConfig.from_settings()
        )

    # ----------------------------- SURESLIP ----------------------------- #

    def build_sure_slips(
        self,
        opportunities: list[BetOpportunity],
        exclude_match_ids: set[str] | frozenset[str] = frozenset(),
    ) -> list[Slip]:
        """Highest-certainty-first greedy build under probability/odds gates."""
        cfg = self.sure_config
        pool = [
            opp
            for opp in opportunities
            if opp.selection.model_probability >= cfg.min_leg_prob
            and opp.decimal_odds <= cfg.max_leg_odds
            and opp.ev_per_unit > 0.0
            and opp.selection.match_id not in exclude_match_ids
        ]
        pool.sort(
            key=lambda opp: (opp.selection.model_probability, opp.ev_per_unit),
            reverse=True,
        )

        slips: list[Slip] = []
        matches_used: set[str] = set()

        for seed in pool:
            if len(slips) >= cfg.max_sure_slips:
                break
            if seed.selection.match_id in matches_used:
                continue

            legs = [seed]
            leg_matches = {seed.selection.match_id}
            combined_odds = seed.decimal_odds
            combined_prob = seed.model_prob

            for candidate in pool:
                if len(legs) >= cfg.max_legs:
                    break
                if candidate.selection.match_id in leg_matches:
                    continue

                new_odds = combined_odds * candidate.decimal_odds
                new_prob = combined_prob * candidate.model_prob
                if new_odds > cfg.max_combined_odds or new_prob < cfg.min_combined_prob:
                    continue

                legs.append(candidate)
                leg_matches.add(candidate.selection.match_id)
                combined_odds = new_odds
                combined_prob = new_prob

            if len(legs) >= cfg.min_legs and combined_odds >= cfg.min_combined_odds:
                slip = Slip(
                    slip_type="SURESLIP",
                    legs=[SlipLeg.from_opportunity(leg) for leg in legs],
                    stake_units=cfg.stake_units,
                )
                if slip.ev_per_unit > 0.0:
                    slips.append(slip)
                    matches_used.update(leg_matches)

        return slips

    # ------------------------------ Singles ------------------------------ #

    def build_singles(
        self,
        opportunities: list[BetOpportunity],
        exclude_match_ids: set[str] | frozenset[str] = frozenset(),
    ) -> list[Slip]:
        cfg = self.config
        eligible = [
            opp
            for opp in opportunities
            if opp.selection.model_probability >= cfg.min_single_prob
            and opp.decimal_odds <= cfg.max_single_odds
            and opp.ev_per_unit > 0.0
            and opp.selection.match_id not in exclude_match_ids
        ]
        eligible.sort(key=lambda opp: opp.ev_per_unit, reverse=True)

        slips: list[Slip] = []
        used_matches: set[str] = set()
        for opp in eligible:
            if len(slips) >= cfg.max_singles:
                break
            if opp.selection.match_id in used_matches:
                continue
            slips.append(
                Slip(
                    slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=cfg.single_stake_units,
                )
            )
            used_matches.add(opp.selection.match_id)
        return slips

    # -------------------------- Standard accas --------------------------- #

    def build_accumulators(
        self,
        opportunities: list[BetOpportunity],
        exclude_match_ids: set[str] | frozenset[str] = frozenset(),
    ) -> list[Slip]:
        cfg = self.config
        pool = [
            opp
            for opp in opportunities
            if opp.selection.model_probability >= cfg.min_acca_leg_prob
            and opp.decimal_odds <= cfg.max_acca_leg_odds
            and opp.ev_per_unit > 0.0
            and opp.selection.match_id not in exclude_match_ids
        ]
        pool.sort(key=lambda opp: opp.ev_per_unit, reverse=True)

        accas: list[Slip] = []
        matches_used_in_accas: set[str] = set()

        for seed in pool:
            if len(accas) >= cfg.max_accas:
                break
            if seed.selection.match_id in matches_used_in_accas:
                continue

            legs = [seed]
            leg_matches = {seed.selection.match_id}
            combined_odds = seed.decimal_odds
            combined_prob = seed.model_prob

            for candidate in pool:
                if len(legs) >= cfg.max_acca_legs:
                    break
                if candidate.selection.match_id in leg_matches:
                    continue

                new_odds = combined_odds * candidate.decimal_odds
                new_prob = combined_prob * candidate.model_prob
                if (
                    new_odds > cfg.max_acca_combined_odds
                    or new_prob < cfg.min_acca_combined_prob
                ):
                    continue

                legs.append(candidate)
                leg_matches.add(candidate.selection.match_id)
                combined_odds = new_odds
                combined_prob = new_prob

            if len(legs) >= 2:
                slip = Slip(
                    slip_type="ACCA",
                    legs=[SlipLeg.from_opportunity(leg) for leg in legs],
                    stake_units=cfg.acca_stake_units,
                )
                if slip.ev_per_unit > 0.0:
                    accas.append(slip)
                    matches_used_in_accas.update(leg_matches)

        return accas

    # ----------------------------- Full slate ---------------------------- #

    def build_all(self, opportunities: list[BetOpportunity]) -> list[Slip]:
        """SURESLIP -> singles -> accas, with match-exclusion chains."""
        sure = self.build_sure_slips(opportunities)
        sure_matches = {leg.match_id for slip in sure for leg in slip.legs}
        singles = self.build_singles(opportunities, exclude_match_ids=sure_matches)
        single_matches = {leg.match_id for slip in singles for leg in slip.legs}
        accas = self.build_accumulators(
            opportunities, exclude_match_ids=sure_matches | single_matches
        )
        return sure + singles + accas
