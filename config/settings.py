"""
Central configuration for sporty-edge.

Priority platforms, league averages, risk gates, SURESLIP rules, staking,
Telegram delivery, and the odds-feed settings all live here.
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

# Data sources for price discovery (feed books). No accounts needed here —
# they only supply reference odds used to detect value.
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
    """Bankroll and staking configuration."""

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

    credits: 1 per region x 1 per market, per sport, per fetch.
    Default (1 region, h2h) = 1 credit/sport/fetch ->
    4 sports x 4 sessions x 30 days = 480 <= 500 free credits.
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


RISK_SETTINGS = RiskSettings()
SURESLIP_SETTINGS = SureSlipSettings()
STAKING_SETTINGS = StakingSettings()
TELEGRAM_SETTINGS = TelegramSettings()
FEED_SETTINGS = FeedSettings()

DEFAULT_UNIT_SIZE: float = STAKING_SETTINGS.unit_size
MAX_ACCA_LEGS: int = RISK_SETTINGS.max_acca_legs


# === sporty-edge credentials (auto-managed by setup_credentials.py) ===
TELEGRAM_SETTINGS = TelegramSettings(
    bot_token="8626261087:AAEewlFtCinkKPjFhkTq_PR7SfYXnk5uFOA",
    chat_id="8202470303",
    enabled=True,
)
FEED_SETTINGS = FeedSettings(
    odds_api_key="8dbc6036b4c640add3b36ff8eab19e50",
)
# === end credentials ===
