"""
Monte Carlo validation layer for the Poisson engine.

The analytic engine is closed-form; Monte Carlo is an independent numerical
implementation.  Empirical frequency p-hat over n trials has standard error
SE = sqrt(p(1-p)/n), so any analytic bug appears as a large z-score:

    z = (p-hat - p) / SE

At n = 200,000, SE at p = 0.5 is about 0.11 percentage points.  A worst-case
|z| below ~3.5 across all markets is strong evidence the engine is correct.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from models.probability_engine import (
    MARKET_1X2,
    MARKET_BTTS,
    MARKET_DOUBLE_CHANCE,
    MARKET_OVER_UNDER,
    OVER_UNDER_LINES,
    ExpectedGoals,
    PoissonMatchModel,
)


def binomial_standard_error(probability: float, n: int) -> float:
    """Standard error of an empirical frequency from ``n`` Bernoulli trials."""
    if n <= 0:
        raise ValueError("n must be positive.")
    p = min(max(probability, 0.0), 1.0)
    return float(np.sqrt(p * (1.0 - p) / n))


@dataclass(frozen=True)
class SimulationResult:
    """Empirical market probabilities from a Monte Carlo run."""

    n_simulations: int
    probabilities: dict[str, float]
    standard_errors: dict[str, float]
    mean_total_goals: float

    def compare_to_model(
        self, model: PoissonMatchModel
    ) -> dict[str, tuple[float, float, float]]:
        """Map of market key -> (model_prob, simulated_prob, z_score)."""
        analytic = model.market_probabilities()
        report: dict[str, tuple[float, float, float]] = {}
        for key, sim_prob in self.probabilities.items():
            model_prob = analytic[key]
            se = self.standard_errors[key]
            z = (sim_prob - model_prob) / se if se > 0 else 0.0
            report[key] = (model_prob, sim_prob, z)
        return report

    def max_abs_z_against(self, model: PoissonMatchModel) -> float:
        return max(abs(z) for _, _, z in self.compare_to_model(model).values())

    def validate_against(self, model: PoissonMatchModel, z_tolerance: float = 3.5) -> bool:
        return self.max_abs_z_against(model) < z_tolerance


class MonteCarloSimulator:
    """Simulates match outcomes from independent Poisson draws (vectorised)."""

    def __init__(
        self,
        expected_goals: ExpectedGoals,
        n_simulations: int = 200_000,
        seed: int | None = None,
    ) -> None:
        if n_simulations < 1_000:
            raise ValueError("Use at least 1,000 simulations for stable estimates.")
        self.expected_goals = expected_goals
        self.n_simulations = int(n_simulations)
        self.seed = seed

    def run(self) -> SimulationResult:
        rng = np.random.default_rng(self.seed)
        n = self.n_simulations

        home = rng.poisson(self.expected_goals.home, size=n)
        away = rng.poisson(self.expected_goals.away, size=n)
        totals: np.ndarray = home + away

        outcomes: dict[str, np.ndarray] = {
            f"{MARKET_1X2}:Home": home > away,
            f"{MARKET_1X2}:Draw": home == away,
            f"{MARKET_1X2}:Away": home < away,
            f"{MARKET_DOUBLE_CHANCE}:1X": home >= away,
            f"{MARKET_DOUBLE_CHANCE}:12": home != away,
            f"{MARKET_DOUBLE_CHANCE}:X2": home <= away,
            f"{MARKET_BTTS}:Yes": (home > 0) & (away > 0),
            f"{MARKET_BTTS}:No": (home == 0) | (away == 0),
        }
        for line in OVER_UNDER_LINES:
            threshold = int(np.floor(line))
            outcomes[f"{MARKET_OVER_UNDER} {line}:Over"] = totals > threshold
            outcomes[f"{MARKET_OVER_UNDER} {line}:Under"] = totals <= threshold

        probabilities = {key: float(mask.mean()) for key, mask in outcomes.items()}
        standard_errors = {
            key: binomial_standard_error(p, n) for key, p in probabilities.items()
        }
        return SimulationResult(
            n_simulations=n,
            probabilities=probabilities,
            standard_errors=standard_errors,
            mean_total_goals=float(totals.mean()),
        )
