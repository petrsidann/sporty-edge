"""
market_scan.py - full-market scanner for ONE match, anchored to the book's own prices.

    python market_scan.py

How it works (the honest multi-market method):
    1. You enter the 1X2 odds your app displays for a match.
    2. They are de-margined (power method) and the Poisson model is FITTED
       so its 1X2 exactly matches the market's own implied probabilities.
    3. The fitted model prices the whole board exactly: O/U every half line,
       team totals, BTTS, double chance (~20 markets).
    4. You paste any app price for any market; the scanner prints fair vs
       app odds and flags positive edges.

Reference = the book's own 1X2, so main-line edges are ~0 by construction;
any flag means the book's OWN markets disagree with each other.  Zero API credits.
"""

from __future__ import annotations

import sys

import numpy as np
from scipy.optimize import least_squares

from models.probability_engine import ExpectedGoals, PoissonMatchModel

OU_LINES: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)
TT_LINES: tuple[float, ...] = (0.5, 1.5, 2.5)
DC_PICKS: tuple[str, ...] = ("1X", "12", "X2")


def _parse_odds_line(raw: str, expected: int, what: str) -> list[float] | None:
    """Parse whitespace/comma separated decimal odds; None on failure."""
    parts = [p for p in raw.replace(",", " ").split() if p]
    if len(parts) != expected:
        print(f"  X expected {expected} numbers for {what}, got {len(parts)}.")
        return None
    try:
        odds = [float(p) for p in parts]
    except ValueError:
        print("  X odds must be numbers, e.g. 1.91 3.80 4.30")
        return None
    if any(o < 1.01 for o in odds):
        print("  X all odds must be >= 1.01.")
        return None
    return odds


def devig(odds: list[float]) -> list[float]:
    """Power-method de-margin: solve sum(q_i^k)=1, p_i = q_i^k renormalised."""
    q = [1.0 / o for o in odds]
    total = sum(q)
    if total <= 1.0:
        return [x / total for x in q] if total > 0 else q
    lo, hi = 1.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if sum(x**mid for x in q) > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    p = [x**k for x in q]
    s = sum(p)
    return [x / s for x in p]


def fit_lambdas(
    p_home: float, p_draw: float | None, p_away: float
) -> tuple[float, float]:
    """Fit (lambda_home, lambda_away) so the model's 1X2 matches the market."""

    def residual(x: np.ndarray) -> list[float]:
        m = PoissonMatchModel(ExpectedGoals(float(x[0]), float(x[1])))
        r = m.one_x_two()
        out = [r["Home"] - p_home]
        if p_draw is not None:
            out.append(r["Draw"] - p_draw)
        return out

    sol = least_squares(
        residual,
        x0=np.array([1.4, 1.1]),
        bounds=(np.array([0.05, 0.05]), np.array([6.0, 6.0])),
    )
    return float(sol.x[0]), float(sol.x[1])


def build_board(model: PoissonMatchModel) -> dict[str, float]:
    """Price the whole board: {market key: model probability}."""
    board: dict[str, float] = {}
    r = model.one_x_two()
    for pick in ("Home", "Draw", "Away"):
        board[f"1X2 {pick}"] = r[pick]
    dc = model.double_chance()
    for pick in DC_PICKS:
        board[f"DC {pick}"] = dc[pick]
    btts = model.both_teams_to_score()
    board["BTTS Yes"] = btts["yes"]
    board["BTTS No"] = btts["no"]
    for line in OU_LINES:
        ou = model.over_under(line)
        board[f"OU {line:g} Over"] = ou["over"]
        board[f"OU {line:g} Under"] = ou["under"]
    for side in ("Home", "Away"):
        pmf = model._home_pmf if side == "Home" else model._away_pmf
        for line in TT_LINES:
            k = int(line)  # half line k+0.5: Over means scoring >= k+1
            over = 1.0 - float(pmf[: k + 1].sum())
            board[f"TT {side} {line:g} Over"] = over
            board[f"TT {side} {line:g} Under"] = 1.0 - over
    return board


def print_board(board: dict[str, float]) -> None:
    print("\n  FULL BOARD - model fair odds (fitted to this book's own 1X2)")
    print("  " + "-" * 44)
    for key, prob in board.items():
        if prob <= 0.001 or prob >= 0.999:
            continue
        print(f"  {key:<24} fair {1.0 / prob:7.2f}")


def main() -> None:
    print("=" * 64)
    print("  sporty-edge market scanner - one match, full board")
    print("=" * 64)
    home = input("  Home team name: ").strip() or "Home"
    away = input("  Away team name: ").strip() or "Away"

    has_draw = True
    raw = input(
        f"\n  1X2 odds for {home} vs {away} from your app\n"
        f"  (e.g. 1.91 3.80 4.30; if no draw, press Enter then give 2 odds): "
    ).strip()
    if not raw:
        has_draw = False
        raw = input("  2-way odds (Home Away): ").strip()
    odds = _parse_odds_line(raw, 3 if has_draw else 2, "1X2")
    if odds is None:
        sys.exit(1)

    p = devig(odds)
    if has_draw:
        p_home, p_draw, p_away = p
    else:
        p_home, p_away = p
        p_draw = None

    lam_h, lam_a = fit_lambdas(p_home, p_draw, p_away)
    model = PoissonMatchModel(ExpectedGoals(lam_h, lam_a))

    print(f"\n  Fitted expected goals: {home} lam {lam_h:.2f} | {away} lam {lam_a:.2f}")
    implied = f"{p_home * 100:.1f}%"
    if p_draw is not None:
        implied += f" / {p_draw * 100:.1f}%"
    implied += f" / {p_away * 100:.1f}%"
    print(f"  Market implied (de-margined): {implied}")

    board = build_board(model)
    print_board(board)

    print(
        "\n  Now paste any app price to check it against fair.\n"
        "  Formats (one per line):\n"
        "    OU 2.5 Over 1.90        TT Home 1.5 Over 1.25\n"
        "    TT Away 0.5 Under 2.65  BTTS Yes 1.80\n"
        "    DC 1X 1.24              1X2 Away 4.30\n"
        "  Press Enter on an empty line to finish."
    )

    checks: list[tuple[str, float, float, float]] = []
    while True:
        try:
            entry = input("  check> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not entry:
            break
        parts = entry.split()
        if len(parts) < 2:
            print("  X format: MARKET ... ODDS  (e.g. 'OU 2.5 Over 1.90')")
            continue
        try:
            app_odds = float(parts[-1])
        except ValueError:
            print("  X last token must be the odds number.")
            continue
        key = " ".join(parts[:-1]).upper().replace("O/U", "OU")
        lookup = {k.upper(): k for k in board}
        kk = lookup.get(key)
        if kk is None:
            print(f"  X unknown market '{key}'. Copy names from the board above.")
            continue
        prob = board[kk]
        ev = prob * app_odds - 1.0
        fair = 1.0 / prob
        verdict = "VALUE" if ev >= 0.02 else ("thin" if ev > 0 else "negative")
        print(
            f"    {kk:<24} app {app_odds:5.2f} | fair {fair:5.2f} | "
            f"EV {ev * 100:+5.1f}%  [{verdict}]"
        )
        checks.append((kk, app_odds, fair, ev))

    if checks:
        print("\n  SUMMARY - sorted by edge")
        print("  " + "-" * 52)
        for kk, app_odds, fair, ev in sorted(checks, key=lambda c: c[3], reverse=True):
            flag = "[+]" if ev >= 0.02 else ("[~]" if ev > 0 else "[-]")
            print(
                f"  {flag} {kk:<24} app {app_odds:5.2f} fair {fair:5.2f} "
                f"EV {ev * 100:+5.1f}%"
            )
        print(
            "\n  Flags are INTERNAL inconsistencies in this book's own pricing -"
            "\n  usually small. Verify the price is still live before staking,"
            "\n  log the slip, and settle the result. The ledger is the judge."
        )


if __name__ == "__main__":
    main()