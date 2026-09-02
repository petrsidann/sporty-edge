"""
Central configuration for sporty-edge.

Priority platforms, league averages, risk gates, SURESLIP rules, staking,
Telegram delivery, odds-feed settings, and ACTION mode all live here.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Platforms
# --------------------------------------------------------------------------- #

# The apps YOU hold accounts with. Every pick sheet loads on one of these.
PLACEABLE_BOOKS: list[str] = [
    "SportyBet",
    "Betika",
    "BetPawa",
    "Betfalme",
]

# First in line when several placeable books are available.
PRIORITY_BOOKS: list[str] = [
    "SportyBet",
    "Betika",
    "BetPawa",
    "Betfalme",
]

# Data sources for price discovery (feed books). No accounts needed here.
SUPPORTED_BOOKS: list[str] = [
    "SportyBet",
    "Betika",
    "BetPawa",
    "Betfalme",
    "WekaWin",
    "BetJam",
    "LuckyPari",
    "MozzartBet",
    "Odibets",
    "1xBet",
    "Betway",
]

# Place a leg when your app's odds are at least reference_odds * (1 - tolerance).
PRICE_TOLERANCE: float = 0.03  # 3%


# --------------------------------------------------------------------------- #
# Target leagues and scoring averages (soccer engine)
# --------------------------------------------------------------------------- #

TARGET_LEAGUES: dict[str, list[str]] = {
    "Europe": [
        "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
        "UEFA Champions League", "Eredivisie", "Primeira Liga",
    ],
    "Africa": [
        "Egyptian Premier League", "Botola Pro (Morocco)",
        "Tunisian Ligue Professionnelle 1", "DStv Premiership (South Africa)",
        "Nigeria Professional Football League", "Kenyan Premier League",
        "Tanzanian Premier League", "Ghana Premier League", "CAF Champions League",
    ],
}

LEAGUE_AVERAGE_GOALS: dict[str, tuple[float, float]] = {
    "Premier League": (1.65, 1.35),
    "La Liga": (1.55, 1.20),
    "Serie A": (1.60, 1.25),
    "Bundesliga": (1.75, 1.35),
    "Ligue 1": (1.55, 1.20),
    "Egyptian Premier League": (1.45, 1.10),
    "DStv Premiership (South Africa)": (1.35, 0.95),
    "Kenyan Premier League": (1.30, 1.00),
}

DEFAULT_LEAGUE_HOME_GOALS: float = 1.55
DEFAULT_LEAGUE_AWAY_GOALS: float = 1.25


# --------------------------------------------------------------------------- #
# Risk gates
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RiskSettings:
    """Quality gates for singles and standard accas."""

    min_edge: float = 0.03
    min_ev_per_unit: float = 0.03

    min_model_prob_single: float = 0.40
    max_single_odds: float = 4.00
    max_singles: int = 3
    single_stake_units: float = 1.0

    max_acca_legs: int = 4
    max_accas: int = 2
    min_acca_leg_prob: float = 0.55
    max_acca_leg_odds: float = 2.50
    min_acca_combined_prob: float = 0.28
    max_acca_combined_odds: float = 9.00
    acca_stake_units: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_edge < 1.0:
            raise ValueError("min_edge must be in [0, 1).")
        if self.min_ev_per_unit < 0.0:
            raise ValueError("min_ev_per_unit must be non-negative.")
        if not 1 <= self.max_acca_legs <= 4:
            raise ValueError("max_acca_legs is capped at 4 by design.")
        if not 0.0 < self.min_model_prob_single < 1.0:
            raise ValueError("min_model_prob_single must be in (0, 1).")
        if not 0.0 < self.min_acca_leg_prob < 1.0:
            raise ValueError("min_acca_leg_prob must be in (0, 1).")
        if not 0.0 < self.min_acca_combined_prob < 1.0:
            raise ValueError("min_acca_combined_prob must be in (0, 1).")
        if self.max_single_odds <= 1.0 or self.max_acca_leg_odds <= 1.0:
            raise ValueError("Odds caps must be greater than 1.0.")
        if self.max_acca_combined_odds <= 1.0:
            raise ValueError("max_acca_combined_odds must be greater than 1.0.")


@dataclass(frozen=True)
class SureSlipSettings:
    """SURESLIP tier — strictest filters in the system."""

    min_legs: int = 4
    max_legs: int = 6
    min_leg_prob: float = 0.85
    max_leg_odds: float = 2.00
    min_combined_prob: float = 0.50
    min_combined_odds: float = 1.0
    max_combined_odds: float = 10.0
    stake_units: float = 1.0
    max_sure_slips: int = 1

    def __post_init__(self) -> None:
        if not 2 <= self.min_legs <= self.max_legs <= 10:
            raise ValueError("Require 2 <= min_legs <= max_legs <= 10.")
        if not 0.0 < self.min_leg_prob < 1.0 or not 0.0 < self.min_combined_prob < 1.0:
            raise ValueError("Sure-slip probability floors must lie in (0, 1).")
        if self.max_leg_odds <= 1.0 or self.max_combined_odds <= 1.0:
            raise ValueError("Odds caps must be greater than 1.0.")
        if self.min_combined_odds < 1.0 or self.min_combined_odds > self.max_combined_odds:
            raise ValueError("min_combined_odds must be >= 1.0 and <= max_combined_odds.")
        if self.stake_units <= 0.0:
            raise ValueError("stake_units must be positive.")
        if self.max_sure_slips < 0:
            raise ValueError("max_sure_slips must be non-negative.")


@dataclass(frozen=True)
class StakingSettings:
    """Bankroll and staking configuration.

    Set initial_bankroll and unit_size to your REAL numbers — stakes print
    in units of unit_size against this bankroll.
    """

    initial_bankroll: float = 10_000.0
    unit_size: float = 100.0
    kelly_fraction: float = 0.25
    max_stake_pct_bankroll: float = 0.02
    max_daily_exposure_pct: float = 0.05
    max_units_per_bet: float = 2.0
    max_drawdown_pct: float = 0.25

    def __post_init__(self) -> None:
        if self.initial_bankroll <= 0 or self.unit_size <= 0:
            raise ValueError("Bankroll and unit size must be positive.")
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1].")
        if not 0.0 < self.max_stake_pct_bankroll < 1.0:
            raise ValueError("max_stake_pct_bankroll must be in (0, 1).")
        if not 0.0 < self.max_daily_exposure_pct <= 1.0:
            raise ValueError("max_daily_exposure_pct must be in (0, 1].")
        if not 0.0 < self.max_drawdown_pct < 1.0:
            raise ValueError("max_drawdown_pct must be in (0, 1).")
        if self.max_units_per_bet <= 0:
            raise ValueError("max_units_per_bet must be positive.")


@dataclass(frozen=True)
class TelegramSettings:
    """Telegram bot credentials (env vars TELEGRAM_* take priority)."""

    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False


@dataclass(frozen=True)
class FeedSettings:
    """The Odds API feed configuration.

    Credit cost per fetch = regions x markets, per sport.
      h2h only          -> 1 credit/sport/fetch -> 4x4 sessions = 480/month
      "h2h,totals"      -> 2 credits/sport/fetch -> 4x4 = 960 (needs paid tier)
      More leagues: swap feed_sports keys.  Softer markets (EFL Championship,
      Eredivisie, Primeira Liga) show more cross-book dispersion than EPL/La
      Liga.  Tennis keys are tournament-scoped and rotate: run
      `python3 -m feeds.oddsapi --list-sports` while a tournament is live and
      swap keys in.
    """

    odds_api_key: str = ""
    regions: str = "eu"
    markets: str = "h2h"
    feed_sports: tuple[str, ...] = (
        "soccer_epl",
        "soccer_spain_la_liga",
        "basketball_nba",
        "baseball_mlb",
    )
    min_books_for_consensus: int = 2
    min_edge_feed: float = 0.02
    min_ev_feed: float = 0.02


@dataclass(frozen=True)
class ActionSettings:
    """ACTION mode — daily picks when no edge is detected.

    When the +EV scan finds nothing, the system ranks every candidate by
    proximity to value (highest edge, even if negative) and emits the best
    ``max_picks`` as ACTION slips at a fixed stake.  Clearly labeled in the
    ledger and on Telegram as no-edge picks, so their ROI is measured
    honestly over time.
    """

    max_picks: int = 2
    stake_units: float = 0.5
    min_odds: float = 1.40
    max_odds: float = 3.50
    min_prob: float = 0.25
    min_books: int = 3

    def __post_init__(self) -> None:
        if self.max_picks < 0:
            raise ValueError("max_picks must be non-negative.")
        if self.stake_units <= 0.0:
            raise ValueError("stake_units must be positive.")
        if not 1.0 < self.min_odds < self.max_odds:
            raise ValueError("Require 1.0 < min_odds < max_odds.")
        if not 0.0 < self.min_prob < 1.0:
            raise ValueError("min_prob must lie in (0, 1).")
        if self.min_books < 1:
            raise ValueError("min_books must be at least 1.")


RISK_SETTINGS = RiskSettings()
SURESLIP_SETTINGS = SureSlipSettings()
STAKING_SETTINGS = StakingSettings()
TELEGRAM_SETTINGS = TelegramSettings()
FEED_SETTINGS = FeedSettings()
ACTION_SETTINGS = ActionSettings()

DEFAULT_UNIT_SIZE: float = STAKING_SETTINGS.unit_size
MAX_ACCA_LEGS: int = RISK_SETTINGS.max_acca_legs