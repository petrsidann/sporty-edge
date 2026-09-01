"""
sporty-edge — the runner. Everything starts here.

    python main.py

Pipeline:
    1. Poisson models for fixtures (dummy demo data — replace with your own).
    2. Monte Carlo validation (200k sims per match).
    3. Platform odds -> positive-EV scan.
    4. SURESLIP + singles + accas, bankroll risk gates.
    5. Platform pick sheets -> console AND Telegram.
    6. Portfolio summary -> console AND Telegram.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from config.settings import (
    DEFAULT_LEAGUE_AWAY_GOALS,
    DEFAULT_LEAGUE_HOME_GOALS,
    LEAGUE_AVERAGE_GOALS,
    RISK_SETTINGS,
    STAKING_SETTINGS,
    SURESLIP_SETTINGS,
    SUPPORTED_BOOKS,
)
from models.monte_carlo import MonteCarloSimulator
from models.probability_engine import (
    MARKET_1X2,
    MARKET_BTTS,
    MARKET_OVER_UNDER,
    PoissonMatchModel,
    TeamStrengths,
    expected_goals_from_strengths,
)
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator, OddsQuote, Selection
from slips.generator import Slip, SlipGenerator, SlipGeneratorConfig
from slips.platform_slip import PlatformSheet, choose_platform
from utils.bankroll import Bankroll
from utils.logger import BetLogger

N_MONTE_CARLO_SIMS = 200_000


# --------------------------------------------------------------------------- #
# Dummy fixtures (demo). Replace strengths with your own estimates for real use.
# --------------------------------------------------------------------------- #

DUMMY_FIXTURES: list[dict[str, Any]] = [
    {
        "match_id": "M1",
        "league": "Premier League",
        "home": "Arsenal",
        "away": "Chelsea",
        "home_strengths": TeamStrengths(attack=1.35, defense=0.80),
        "away_strengths": TeamStrengths(attack=1.00, defense=1.05),
    },
    {
        "match_id": "M2",
        "league": "La Liga",
        "home": "Real Betis",
        "away": "Sevilla",
        "home_strengths": TeamStrengths(attack=1.05, defense=0.95),
        "away_strengths": TeamStrengths(attack=0.95, defense=1.00),
    },
    {
        "match_id": "M3",
        "league": "Serie A",
        "home": "Atalanta",
        "away": "Torino",
        "home_strengths": TeamStrengths(attack=1.30, defense=0.85),
        "away_strengths": TeamStrengths(attack=0.85, defense=1.15),
    },
    {
        "match_id": "M4",
        "league": "Bundesliga",
        "home": "Bayer Leverkusen",
        "away": "Mainz 05",
        "home_strengths": TeamStrengths(attack=1.40, defense=0.90),
        "away_strengths": TeamStrengths(attack=0.90, defense=1.20),
    },
    {
        "match_id": "M5",
        "league": "Egyptian Premier League",
        "home": "Al Ahly",
        "away": "Pyramids FC",
        "home_strengths": TeamStrengths(attack=1.45, defense=0.75),
        "away_strengths": TeamStrengths(attack=1.00, defense=0.95),
    },
    {
        "match_id": "M6",
        "league": "DStv Premiership (South Africa)",
        "home": "Mamelodi Sundowns",
        "away": "SuperSport United",
        "home_strengths": TeamStrengths(attack=1.50, defense=0.70),
        "away_strengths": TeamStrengths(attack=0.80, defense=1.00),
    },
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _banner(title: str) -> None:
    line = "=" * 74
    print(f"\n{line}\n  {title}\n{line}")


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _league_averages(league: str) -> tuple[float, float]:
    return LEAGUE_AVERAGE_GOALS.get(
        league, (DEFAULT_LEAGUE_HOME_GOALS, DEFAULT_LEAGUE_AWAY_GOALS)
    )


def _synthesise_quotes(
    fair_probability: float,
    rng: np.random.Generator,
    n_books: int = 5,
) -> list[OddsQuote]:
    """Dummy quotes around fair price. Replace with real odds for real use."""
    fair_odds = 1.0 / fair_probability
    books = rng.choice(
        SUPPORTED_BOOKS, size=min(n_books, len(SUPPORTED_BOOKS)), replace=False
    )
    quotes: list[OddsQuote] = []
    for book in books:
        noise = rng.uniform(-0.045, 0.065)
        odds = max(1.01, round(fair_odds * (1.0 + noise), 2))
        quotes.append(OddsQuote(book=str(book), decimal_odds=odds))
    return quotes


def _print_value_table(opportunities: list[BetOpportunity], limit: int = 10) -> None:
    header = (
        f"  {'#':>2}  {'Match':<30} {'Pick':<22} {'Book':<11} "
        f"{'Odds':>5} {'Model':>6} {'Edge':>6} {'EV/u':>6} {'f*':>5}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, opp in enumerate(opportunities[:limit], start=1):
        pick = f"{opp.selection.market}->{opp.selection.selection}"
        print(
            f"  {i:>2}  {_trunc(opp.selection.match_label, 30):<30} "
            f"{_trunc(pick, 22):<22} {opp.book:<11} {opp.decimal_odds:>5.2f} "
            f"{opp.selection.model_probability * 100:>5.1f}% "
            f"{opp.edge * 100:>+5.1f}% {opp.ev_per_unit * 100:>+5.1f}% "
            f"{opp.kelly_full * 100:>4.1f}%"
        )
    if len(opportunities) > limit:
        print(f"  … and {len(opportunities) - limit} more (showing top {limit} by EV).")


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #

def main() -> None:
    tg = TelegramNotifier()
    _banner("SPORTY-EDGE  |  probability -> EV -> SURESLIP -> platform sheets")

    if tg.is_configured:
        tg.send("🟢 sporty-edge run started.")
        print("  Telegram: ENABLED — reports and pick sheets will be sent.")
    else:
        print(
            "  Telegram: not configured (see README to enable — system still "
            "runs fully in console)."
        )

    # ------------------------- STEP 1: models --------------------------- #
    _banner("STEP 1/6 — Building independent Poisson models (dummy fixtures)")
    models: dict[str, PoissonMatchModel] = {}
    for fx in DUMMY_FIXTURES:
        avg_home, avg_away = _league_averages(fx["league"])
        expected_goals = expected_goals_from_strengths(
            avg_home, avg_away, fx["home_strengths"], fx["away_strengths"]
        )
        model = PoissonMatchModel(expected_goals)
        models[fx["match_id"]] = model

        p = model.market_probabilities()
        label = f"{fx['home']} vs {fx['away']}"
        print(
            f"  {fx['match_id']}  {_trunc(label, 34):<34} "
            f"λ {model.expected_goals.home:.2f}–{model.expected_goals.away:.2f}  |  "
            f"1 {p[f'{MARKET_1X2}:Home'] * 100:4.1f}%  "
            f"X {p[f'{MARKET_1X2}:Draw'] * 100:4.1f}%  "
            f"2 {p[f'{MARKET_1X2}:Away'] * 100:4.1f}%  |  "
            f"O2.5 {p[f'{MARKET_OVER_UNDER} 2.5:Over'] * 100:4.1f}%  "
            f"BTTS {p[f'{MARKET_BTTS}:Yes'] * 100:4.1f}%"
        )

    # --------------------- STEP 2: Monte Carlo --------------------------- #
    _banner(f"STEP 2/6 — Monte Carlo validation ({N_MONTE_CARLO_SIMS:,} sims per match)")
    for i, fx in enumerate(DUMMY_FIXTURES):
        model = models[fx["match_id"]]
        simulator = MonteCarloSimulator(
            model.expected_goals, n_simulations=N_MONTE_CARLO_SIMS, seed=20240101 + i
        )
        result = simulator.run()
        worst_z = result.max_abs_z_against(model)
        status = "PASS" if worst_z < 3.5 else "REVIEW"
        analytic_total = model.expected_goals.home + model.expected_goals.away
        print(
            f"  {fx['match_id']}: worst deviation {worst_z:4.2f}σ ({status})  |  "
            f"mean total goals {result.mean_total_goals:.2f} "
            f"(analytic {analytic_total:.2f})"
        )

    # ----------------------- STEP 3: EV scan ----------------------------- #
    _banner("STEP 3/6 — Scanning platform odds for positive EV")
    comparator = OddsComparator(
        min_edge=RISK_SETTINGS.min_edge,
        min_ev_per_unit=RISK_SETTINGS.min_ev_per_unit,
    )

    rng = np.random.default_rng(42)
    candidates: list[tuple[Selection, list[OddsQuote]]] = []

    for fx in DUMMY_FIXTURES:
        model = models[fx["match_id"]]
        label = f"{fx['home']} vs {fx['away']}"
        for market_key, prob in model.market_probabilities().items():
            market, _, pick = market_key.partition(":")
            selection = Selection(
                match_id=fx["match_id"],
                match_label=label,
                league=fx["league"],
                market=market,
                selection=pick,
                model_probability=prob,
            )
            quotes = _synthesise_quotes(prob, rng)
            candidates.append((selection, quotes))

    value_bets = comparator.find_value_bets(candidates)
    print(f"  Candidate selections evaluated : {len(candidates)}")
    print(
        f"  Positive-EV opportunities kept : {len(value_bets)} "
        f"(min edge {RISK_SETTINGS.min_edge:.1%}, "
        f"min EV {RISK_SETTINGS.min_ev_per_unit:.1%})"
    )
    if not value_bets:
        print("  No value found with current thresholds — nothing to bet today.")
        if tg.is_configured:
            tg.send("⚪ sporty-edge: no value found today — nothing to bet.")
        return
    _print_value_table(value_bets)

    # --------------------- STEP 4: build slips --------------------------- #
    _banner("STEP 4/6 — Building slips (SURESLIP first, then singles, then accas)")
    print(
        f"  SURESLIP policy: ≥{SURESLIP_SETTINGS.min_leg_prob:.0%} model prob per pick, "
        f"{SURESLIP_SETTINGS.min_legs}–{SURESLIP_SETTINGS.max_legs} picks, "
        f"slip win-prob floor {SURESLIP_SETTINGS.min_combined_prob:.0%}  |  "
        f"singles flat {RISK_SETTINGS.single_stake_units:.1f} u  |  "
        f"accas {RISK_SETTINGS.acca_stake_units:.1f} u"
    )

    bankroll = Bankroll()
    print(
        f"  Bankroll {bankroll.current_bankroll:,.0f}  |  "
        f"unit {bankroll.unit_size:,.0f}  |  "
        f"max daily exposure {STAKING_SETTINGS.max_daily_exposure_pct:.0%}"
    )

    generator = SlipGenerator(SlipGeneratorConfig.from_settings())
    slips = generator.build_all(value_bets)
    if not slips:
        print("  No slips met the conservative quality bar — stand down.")
        if tg.is_configured:
            tg.send("⚪ sporty-edge: no slips cleared the quality bar — stand down.")
        return

    logger = BetLogger()
    placed_slips: list[Slip] = []
    bet_ids: dict[str, str] = {}
    for slip in slips:
        if not bankroll.can_place(slip.stake_units):
            print(f"  !! Risk control blocked slip {slip.slip_id} (exposure limit).")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip)
        bet_ids[slip.slip_id] = bet_id
        placed_slips.append(slip)
        print()
        print(slip.render())
        print(f"  -> logged as {bet_id} (status PENDING)")

    # ------------------- STEP 5: platform sheets ------------------------- #
    _banner("STEP 5/6 — Platform pick sheets (console + Telegram)")
    for slip in placed_slips:
        sheet: PlatformSheet | None = choose_platform(slip)
        if sheet is not None:
            text = sheet.render(bet_id=bet_ids[slip.slip_id])
            print(text)
            if tg.is_configured:
                tg.send_sheet(sheet, bet_id=bet_ids[slip.slip_id])
        else:
            print(slip.render())
        print()

    # --------------------- STEP 6: summary ------------------------------- #
    _banner("STEP 6/6 — Portfolio summary")
    sure = [s for s in placed_slips if s.slip_type == "SURESLIP"]
    singles = [s for s in placed_slips if s.slip_type == "SINGLE"]
    accas = [s for s in placed_slips if s.slip_type == "ACCA"]
    total_stake = sum(s.stake_units for s in placed_slips)
    expected_profit = sum(s.stake_units * s.ev_per_unit for s in placed_slips)

    for slip in placed_slips:
        print(f"  {slip.summary_line()}")

    print()
    print(
        f"  Slips placed : {len(placed_slips)}  "
        f"(sureslip: {len(sure)}, singles: {len(singles)}, accas: {len(accas)})"
    )
    print(
        f"  Total stake  : {total_stake:.2f} units "
        f"= {bankroll.units_to_currency(total_stake):,.0f} currency"
    )
    if total_stake > 0:
        print(
            f"  Expected P/L : {expected_profit:+.2f} units "
            f"({expected_profit / total_stake * 100:+.1f}% of turnover)"
        )
    print(f"  Bankroll     : {bankroll.summary()}")
    print(f"  Ledger file  : {logger.path}")
    print(f"  Metrics      : {logger.metrics()}")

    if tg.is_configured:
        lines = ["📊 sporty-edge portfolio summary"]
        for slip in placed_slips:
            lines.append(f"• {slip.summary_line()}")
        lines.append(f"Total stake: {total_stake:.2f}u | Expected P/L: {expected_profit:+.2f}u")
        lines.append("Next: load each sheet on its named platform, then register the code:")
        lines.append("BetLogger().attach_code('BET-XXXXXXXX','Platform','CODE')")
        tg.send("\n".join(lines))

    print(
        "\n  1. Load each pick sheet on the platform it names.\n"
        "  2. Register the booking code the platform gives you:\n"
        '      logger.attach_code("BET-XXXXXXXX", "SportyBet", "THE-CODE")\n'
        "  3. After the matches finish, settle results:\n"
        '      logger.settle("BET-XXXXXXXX", "WIN")     # or "LOSS" / "VOID"\n'
        "      print(logger.metrics())\n"
        "      print(logger.breakdown(by='slip_type'))"
    )


if __name__ == "__main__":
    main()
