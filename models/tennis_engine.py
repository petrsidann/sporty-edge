"""
Tennis match probability engine — i.i.d. point model.

Inputs (per match, in data/fixtures.csv):
    pa : P(home player wins a point on their own serve)
    pb : P(away player wins a point on their own serve)

Tour-level serve point-win rates sit around 0.60-0.68.  The DIFFERENCE
between the two players is what moves the probabilities.

Math chain (exact, documented):
    Game      closed form from the deuce recursion  D = p^2 / (1 - 2pq)
    Tiebreak  exact point-level recursion with real serve alternation
    Set       dynamic program over game score (to 6, win by 2, TB at 6-6)
    Match     best-of-3: s^2 (3 - 2s)  |  best-of-5: s^3 (1 + 3(1-s) + 6(1-s)^2)
where s = P(win one set).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

MARKET_ML = "ML"


@dataclass(frozen=True)
class TennisInput:
    """Serve-stat inputs for one tennis match."""

    home_player: str
    away_player: str
    pa: float
    pb: float
    best_of: int = 3

    def __post_init__(self) -> None:
        if not 0.05 <= self.pa <= 0.95 or not 0.05 <= self.pb <= 0.95:
            raise ValueError("Serve point probabilities must lie in [0.05, 0.95].")
        if self.best_of not in (3, 5):
            raise ValueError("best_of must be 3 or 5.")


def game_win_prob(p: float) -> float:
    """P(the server wins a service game) given point-win probability p.

    Win to 0/15/30: p^4, 4 p^4 q, 10 p^4 q^2.
    Reach deuce 3-3 (20 p^3 q^3) then win it: D = p^2 / (1 - 2pq),
    from the recursion D = p^2 + 2pqD.
    """
    q = 1.0 - p
    deuce = (p * p) / (1.0 - 2.0 * p * q)
    return p**4 * (1.0 + 4.0 * q + 10.0 * q * q) + 20.0 * p**3 * q**3 * deuce


@lru_cache(maxsize=None)
def tiebreak_win_prob(pa: float, pb: float) -> float:
    """P(home player wins a tiebreak to 7, win by 2), exact serve order."""

    def point_for_home(t: int) -> float:
        # Real tiebreak serving: home serves point 0, then away serves
        # points 1-2, home serves 3-4, away 5-6, ... Home serves point t
        # iff t == 0 or ((t - 1) // 2) % 2 == 1.
        if t == 0 or ((t - 1) // 2) % 2 == 1:
            return pa
        return 1.0 - pb

    @lru_cache(maxsize=None)
    def f(a: int, b: int) -> float:
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        p = point_for_home(a + b)
        return p * f(a + 1, b) + (1.0 - p) * f(a, b + 1)

    return f(0, 0)


@lru_cache(maxsize=None)
def set_win_prob(pa: float, pb: float) -> float:
    """P(home player wins one set), home player serving first."""
    ga = game_win_prob(pa)
    gb = game_win_prob(pb)

    @lru_cache(maxsize=None)
    def f(a: int, b: int, s: int) -> float:
        # s: 0 -> home serves this game, 1 -> away serves this game.
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:
            return tiebreak_win_prob(pa, pb)
        p_game = ga if s == 0 else 1.0 - gb
        return p_game * f(a + 1, b, 1) + (1.0 - p_game) * f(a, b + 1, 0)

    return f(0, 0, 0)


def match_win_prob(pa: float, pb: float, best_of: int) -> float:
    """P(home player wins the match), best of 3 or 5 sets."""
    s = set_win_prob(pa, pb)
    if best_of == 3:
        return s * s * (3.0 - 2.0 * s)
    return s**3 * (1.0 + 3.0 * (1.0 - s) + 6.0 * (1.0 - s) ** 2)


class TennisMatchModel:
    """Moneyline probabilities for one tennis match.  Interface-compatible
    with the soccer PoissonMatchModel (market_probabilities), so the whole
    downstream system works on tennis without any changes."""

    def __init__(self, spec: TennisInput) -> None:
        self.spec = spec
        self.home_hold_rate = game_win_prob(spec.pa)
        self.away_hold_rate = game_win_prob(spec.pb)
        self.set_prob = set_win_prob(spec.pa, spec.pb)
        self.match_prob = match_win_prob(spec.pa, spec.pb, spec.best_of)

    def market_probabilities(self) -> dict[str, float]:
        p = self.match_prob
        return {f"{MARKET_ML}:Home": p, f"{MARKET_ML}:Away": 1.0 - p}

    def summary(self) -> str:
        return (
            f"hold {self.home_hold_rate:.0%}/{self.away_hold_rate:.0%} | "
            f"set {self.set_prob:.0%} | match {self.match_prob:.0%}"
        )