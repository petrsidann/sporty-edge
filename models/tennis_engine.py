"""
Tennis match probability engine — i.i.d. point model.

Inputs (per match, in data/fixtures.csv):
    pa : P(home player wins a point on their own serve)
    pb : P(away player wins a point on their own serve)

Math chain (exact, documented):
    Game      closed form from the deuce recursion  D = p^2 / (1 - 2pq)
    Tiebreak  point-level recursion, exact serve order, TIES FOLDED INTO A
              CLOSED FORM at 6-6 (see tiebreak_win_prob — a win-by-2
              tiebreak has no score cap, so a naive recursion diverges)
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
    """P(home player wins a tiebreak to 7, win by 2), exact serve order.

    Serving pattern (0-indexed points): home serves point 0, away serves
    1-2, home 3-4, away 5-6, ... — so home serves t when
    t == 0 or ((t - 1) // 2) % 2 == 1.

    CRITICAL FIX (was a RecursionError): a win-by-2 tiebreak has NO score
    cap — ties at 6-6, 7-7, 8-8... are all reachable.  Any tie (k, k),
    k >= 6, is therefore folded into an exact closed form instead of
    recursing.  With pair-serves (both points of the pair by one player):

        w1 = pa * (1 - pb)            P(home takes both, home-first pair)
        w2 = (1 - pa) * pb            P(home takes both, away-first pair)
        s  = pa*pb + (1-pa)*(1-pb)    P(split, either order)

        A = (w1 + s*w2) / (1 - s^2)   P(win | tie, next pair home-first)
        B = (w2 + s*w1) / (1 - s^2)   P(win | tie, next pair away-first)

    Whether the pair after a tie is home-first alternates with (a+b) mod 4
    (verified against the serve pattern: tie at total 12 -> home-first,
    14 -> away-first, 16 -> home-first, ...).
    """

    def point_for_home(t: int) -> float:
        if t == 0 or ((t - 1) // 2) % 2 == 1:
            return pa
        return 1.0 - pb

    w1 = pa * (1.0 - pb)
    w2 = (1.0 - pa) * pb
    s = pa * pb + (1.0 - pa) * (1.0 - pb)
    denom = 1.0 - s * s

    if denom <= 0.0:
        # Degenerate extremes (pa or pb at a bound): simple alternating.
        a_deuce = w1 / (w1 + w2) if (w1 + w2) > 0.0 else 0.5
        b_deuce = w2 / (w1 + w2) if (w1 + w2) > 0.0 else 0.5
    else:
        a_deuce = (w1 + s * w2) / denom
        b_deuce = (w2 + s * w1) / denom

    @lru_cache(maxsize=None)
    def f(a: int, b: int) -> float:
        if a >= 6 and b >= 6 and a == b:
            # Tie at 6-6 or beyond: closed form.  Next pair is home-first
            # exactly when (a + b) % 4 == 0 (checked against serve order).
            return a_deuce if (a + b) % 4 == 0 else b_deuce
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        p = point_for_home(a + b)
        return p * f(a + 1, b) + (1.0 - p) * f(a, b + 1)

    return f(0, 0)


@lru_cache(maxsize=None)
def set_win_prob(pa: float, pb: float) -> float:
    """P(home player wins one set), home player serving first.

    Games alternate serve each game (tracked by s).  At 6-6 the set goes
    to a tiebreak.  Game scores never exceed 7 because (6,6) consumes the
    tiebreak, so this recursion terminates in at most ~13 games.
    """
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