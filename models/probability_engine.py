"""
Independent Poisson probability engine for football (soccer) matches.

Mathematical background
=======================
Home goals H and away goals A are modelled as independent Poisson variables:

    P(H = i) = e^(-lambda_home) * lambda_home^i / i!
    P(A = j) = e^(-lambda_away) * lambda_away^j / j!
    P(H = i, A = j) = P(H = i) * P(A = j)

All markets are derived exactly from the joint score matrix:

1X2      : lower triangle (home), diagonal (draw), upper triangle (away)
O/U x.5  : P(total <= k) for Under k+0.5
BTTS     : (1 - e^(-lambda_home)) * (1 - e^(-lambda_away))
DC       : sums of 1X2 outcomes

Known limitation: plain independent Poisson slightly under-prices 0-0/1-1;
the Dixon-Coles correction is a planned extension.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

MARKET_1X2 = "1X2"
MARKET_DOUBLE_CHANCE = "DC"
MARKET_OVER_UNDER = "O/U"
MARKET_BTTS = "BTTS"

#: Total-goals lines priced by the engine (half lines only).
OVER_UNDER_LINES: tuple[float, ...] = (1.5, 2.5, 3.5)


@dataclass(frozen=True)
class TeamStrengths:
    """Attack/defense multipliers relative to league average (1.0 = average).

    ``defense`` > 1 means the team concedes more than average.
    """

    attack: float
    defense: float

    def __post_init__(self) -> None:
        if self.attack <= 0 or self.defense <= 0:
            raise ValueError("TeamStrengths multipliers must be strictly positive.")


@dataclass(frozen=True)
class ExpectedGoals:
    """Expected (mean) goals for the home and away team in one match."""

    home: float
    away: float

    def __post_init__(self) -> None:
        if self.home <= 0 or self.away <= 0:
            raise ValueError("Expected goals must be strictly positive.")
        if self.home > 10 or self.away > 10:
            raise ValueError("Expected goals > 10 is not realistic for football.")


def expected_goals_from_strengths(
    league_avg_home_goals: float,
    league_avg_away_goals: float,
    home: TeamStrengths,
    away: TeamStrengths,
) -> ExpectedGoals:
    """Derive expected goals via the standard multiplicative parametrisation.

        lambda_home = league_avg_home_goals * home.attack * away.defense
        lambda_away = league_avg_away_goals * away.attack * home.defense
    """
    lambda_home = league_avg_home_goals * home.attack * away.defense
    lambda_away = league_avg_away_goals * away.attack * home.defense
    return ExpectedGoals(home=lambda_home, away=lambda_away)


class PoissonMatchModel:
    """Independent Poisson model for one match; every market derived exactly."""

    def __init__(self, expected_goals: ExpectedGoals, max_goals: int = 10) -> None:
        if not 6 <= max_goals <= 20:
            raise ValueError("max_goals must be between 6 and 20.")

        self.expected_goals = expected_goals
        self.max_goals = max_goals

        goals = np.arange(max_goals + 1)
        self._home_pmf: np.ndarray = poisson.pmf(goals, expected_goals.home)
        self._away_pmf: np.ndarray = poisson.pmf(goals, expected_goals.away)

        # Joint exact-score matrix via independence: [i, j] = P_H(i) * P_A(j).
        self.score_matrix: np.ndarray = np.outer(self._home_pmf, self._away_pmf)

        # Renormalise the tiny truncation mass so all markets sum to 1.
        if self.score_matrix.sum() > 0.0:
            self.score_matrix /= self.score_matrix.sum()

        self._totals_grid: np.ndarray = np.add.outer(
            np.arange(max_goals + 1), np.arange(max_goals + 1)
        )

    # ---------------------------- 1X2 ---------------------------------- #

    @property
    def home_win_prob(self) -> float:
        return float(np.tril(self.score_matrix, k=-1).sum())

    @property
    def draw_prob(self) -> float:
        return float(np.trace(self.score_matrix))

    @property
    def away_win_prob(self) -> float:
        return float(np.triu(self.score_matrix, k=1).sum())

    def one_x_two(self) -> dict[str, float]:
        return {
            "Home": self.home_win_prob,
            "Draw": self.draw_prob,
            "Away": self.away_win_prob,
        }

    # ------------------------- Double chance ---------------------------- #

    def double_chance(self) -> dict[str, float]:
        home, draw, away = self.home_win_prob, self.draw_prob, self.away_win_prob
        return {"1X": home + draw, "12": home + away, "X2": draw + away}

    # --------------------------- O/U lines ------------------------------ #

    def over_under(self, line: float = 2.5) -> dict[str, float]:
        if line <= 0:
            raise ValueError("Line must be positive.")
        if abs((line * 2) % 2 - 1) > 1e-9:
            raise ValueError(
                "Only half lines (x.5) are supported; integer lines introduce "
                "push/void semantics this model does not price."
            )
        threshold = int(np.floor(line))
        under = float(self.score_matrix[self._totals_grid <= threshold].sum())
        return {"over": 1.0 - under, "under": under}

    # ------------------------------ BTTS -------------------------------- #

    def both_teams_to_score(self) -> dict[str, float]:
        yes = float(self.score_matrix[1:, 1:].sum())
        return {"yes": yes, "no": 1.0 - yes}

    # ------------------------- Full market map --------------------------- #

    def market_probabilities(self) -> dict[str, float]:
        """All markets as {"MARKET:Pick": probability}, shared with Monte Carlo."""
        dc = self.double_chance()
        btts = self.both_teams_to_score()

        probs: dict[str, float] = {
            f"{MARKET_1X2}:Home": self.home_win_prob,
            f"{MARKET_1X2}:Draw": self.draw_prob,
            f"{MARKET_1X2}:Away": self.away_win_prob,
            f"{MARKET_DOUBLE_CHANCE}:1X": dc["1X"],
            f"{MARKET_DOUBLE_CHANCE}:12": dc["12"],
            f"{MARKET_DOUBLE_CHANCE}:X2": dc["X2"],
            f"{MARKET_BTTS}:Yes": btts["yes"],
            f"{MARKET_BTTS}:No": btts["no"],
        }
        for line in OVER_UNDER_LINES:
            ou = self.over_under(line)
            probs[f"{MARKET_OVER_UNDER} {line}:Over"] = ou["over"]
            probs[f"{MARKET_OVER_UNDER} {line}:Under"] = ou["under"]
        return probs
