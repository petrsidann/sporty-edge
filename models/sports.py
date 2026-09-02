"""
Additional sport engines: baseball (log5) and margin sports (basketball,
American football).  Both expose market_probabilities() with the same
interface as the soccer engine, so the comparator, slip builder, platform
sheets, Telegram delivery and ledger all work across every sport unchanged.

Baseball — log5 (Bradley-Terry):
    Inputs: adjusted win rates a, b in (0,1): season win %, Pythagorean
    (run-differential) expectation, or your own ratings.
        P(A beats B) = (a - a*b) / (a + b - 2*a*b)
    Optional home_field bump added before clipping.

Margin sports — normal margin model:
    margin ~ Normal(mu, sigma); mu from your power ratings (home view).
    P(home win) = Phi(mu / sigma).
    Calibration guidance: NBA sigma ~ 12, NFL ~ 13.5.
"""

from __future__ import annotations

from scipy.stats import norm

MARKET_ML = "ML"


class Log5MatchModel:
    """Baseball moneyline probabilities from adjusted team win rates."""

    def __init__(
        self,
        home_win_rate: float,
        away_win_rate: float,
        home_field: float = 0.0,
    ) -> None:
        for rate in (home_win_rate, away_win_rate):
            if not 0.05 <= rate <= 0.95:
                raise ValueError("Win rates must lie in [0.05, 0.95].")
        if not -0.1 <= home_field <= 0.1:
            raise ValueError("home_field must lie in [-0.1, 0.1].")

        a, b = home_win_rate, away_win_rate
        p = (a - a * b) / (a + b - 2.0 * a * b) + home_field
        p = min(max(p, 1e-6), 1.0 - 1e-6)
        self.home_win_prob = p
        self.away_win_prob = 1.0 - p

    def market_probabilities(self) -> dict[str, float]:
        return {
            f"{MARKET_ML}:Home": self.home_win_prob,
            f"{MARKET_ML}:Away": self.away_win_prob,
        }


class MarginMatchModel:
    """Moneyline for sports naturally modelled by a scoring margin."""

    def __init__(self, expected_margin: float, sigma: float = 13.0) -> None:
        if sigma <= 0:
            raise ValueError("sigma must be positive.")
        self.expected_margin = expected_margin
        self.sigma = sigma
        self.home_win_prob = float(norm.cdf(expected_margin / sigma))

    def market_probabilities(self) -> dict[str, float]:
        return {
            f"{MARKET_ML}:Home": self.home_win_prob,
            f"{MARKET_ML}:Away": 1.0 - self.home_win_prob,
        }