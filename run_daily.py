"""
run_daily.py — the session runner.

One betting session:
    1. Detect (or take --session) the current session.
    2. Pull today's events + prices from The Odds API feed (automatic).
       If the feed is unconfigured or down -> fall back to your CSV files.
    3. Consensus-EV scan (feed) or model-EV scan (CSV) -> value bets.
    4. Build slips with stakes from the aggression tier.
    5. Platform pick sheets -> console + Telegram, tagged with the session.
    6. Ledger entries tagged with the session for per-session breakdowns.

Usage:
    python run_daily.py                 # auto-detect session
    python run_daily.py --session evening
"""

from __future__ import annotations

import sys
from dataclasses import replace

from config.settings import FEED_SETTINGS, RISK_SETTINGS
from data.loader import (
    build_candidates,
    fixture_label,
    load_fixtures,
    load_odds,
)
from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipGenerator, SlipGeneratorConfig, SureSlipConfig
from slips.platform_slip import PlatformSheet, choose_platform
from utils.aggression import current_tier
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import Session, by_name, detect


def _banner(title: str) -> None:
    line = "=" * 74
    print(f"\n{line}\n  {title}\n{line}")


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_value_table(opportunities: list[BetOpportunity], limit: int = 12) -> None:
    header = (
        f"  {'#':>2}  {'Match':<30} {'Pick':<16} {'Best Book':<12} "
        f"{'Odds':>5} {'Model':>6} {'Edge':>6} {'EV/u':>6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, opp in enumerate(opportunities[:limit], start=1):
        pick = f"{opp.selection.market}->{opp.selection.selection}"
        print(
            f"  {i:>2}  {_trunc(opp.selection.match_label, 30):<30} "
            f"{_trunc(pick, 16):<16} {opp.book:<12} {opp.decimal_odds:>5.2f} "
            f"{opp.selection.model_probability * 100:>5.1f}% "
            f"{opp.edge * 100:>+5.1f}% {opp.ev_per_unit * 100:>+5.1f}%"
        )
    if len(opportunities) > limit:
        print(f"  … and {len(opportunities) - limit} more (top {limit} by EV).")


def _from_feed() -> tuple[list[tuple[BetOpportunity, None]] | None, list]:
    """Try the automated feed; returns (candidates, source_label) or ([], 'FEED')."""
    feed = OddsApiFeed()
    if not feed.is_configured:
        print("  Feed: no API key configured (ODDS_API_KEY / FeedSettings).")
        return None
    print(f"  Feed: fetching {len(feed.sports)} sport(s) ...")
    candidates = feed.collect()
    print(f"  Feed: {len(candidates)} priced selections from consensus.")
    return candidates


def main() -> None:
    # ---------- session ---------- #
    session: Session | None = None
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        if idx + 1 < len(sys.argv):
            session = by_name(sys.argv[idx + 1])
        if session is None:
            print("  Unknown --session value. Options: morning afternoon evening midnight")
            return
    if session is None:
        session = detect()
    tg = TelegramNotifier()

    _banner(f"SPORTY-EDGE  |  {session.emoji} {session.name} SESSION")

    logger = BetLogger()
    tier = current_tier(logger.metrics())
    print(f"  Aggression tier: [{tier.level}] {tier.name} — {tier.note}")

    if tg.is_configured:
        tg.send(
            f"{session.emoji} sporty-edge {session.name} session starting — "
            f"tier [{tier.level}] {tier.name}"
        )

    # ---------- data: feed first, CSV fallback ---------- #
    source = "FEED"
    candidates = None
    models: dict = {}
    try:
        candidates = _from_feed()
    except Exception as exc:  # the session must never die on the feed
        print(f"  Feed failed unexpectedly: {exc!r}")
        candidates = None

    if not candidates:
        source = "CSV"
        print("  Falling back to CSV inputs (data/fixtures.csv + data/odds.csv).")
        fixtures = load_fixtures()
        if not fixtures:
            print("  No fixtures in CSV either. Nothing to price this session.")
            if tg.is_configured:
                tg.send(f"{session.emoji} {session.name}: no data source available.")
            return
        models, candidates = build_candidates(fixtures, load_odds())
        if not candidates:
            print("  CSV produced no priced selections (missing odds rows?).")
            if tg.is_configured:
                tg.send(f"{session.emoji} {session.name}: no odds entered.")
            return

    print(f"  Data source    : {source}")

    # ---------- model read-out (CSV mode only; feed mode uses consensus) -- #
    if source == "CSV" and models:
        _banner("Model read-out (per fixture, top-4 markets)")
        for row in load_fixtures():
            mid = (row.get("match_id") or "").strip()
            model = models.get(mid)
            if model is None:
                continue
            probs = model.market_probabilities()
            top4 = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:4]
            summary = ", ".join(f"{k} {v * 100:.1f}%" for k, v in top4)
            extra = getattr(model, "summary", None)
            detail = f"  |  {extra()}" if callable(extra) else ""
            print(f"  {mid}  {_trunc(fixture_label(row), 28):<28}  {summary}{detail}")

    # ---------- EV scan ---------- #
    _banner("Positive-EV scan")
    if source == "FEED":
        min_edge, min_ev = FEED_SETTINGS.min_edge_feed, FEED_SETTINGS.min_ev_feed
        print("  (feed mode: model probabilities = cross-book no-vig consensus)")
    else:
        min_edge, min_ev = RISK_SETTINGS.min_edge, RISK_SETTINGS.min_ev_per_unit

    comparator = OddsComparator(min_edge=min_edge, min_ev_per_unit=min_ev)
    value_bets = comparator.find_value_bets(candidates)
    print(
        f"  Positive-EV opportunities: {len(value_bets)} "
        f"(min edge {min_edge:.1%}, min EV {min_ev:.1%})"
    )
    if not value_bets:
        print("  No value this session — the professional answer is no bets.")
        if tg.is_configured:
            tg.send(f"{session.emoji} {session.name}: no positive-EV found — no bets.")
        return
    _print_value_table(value_bets)

    # ---------- slips ---------- #
    _banner("Slip construction (stake sizes from your aggression tier)")
    gen_cfg = replace(
        SlipGeneratorConfig.from_settings(),
        single_stake_units=tier.single_stake_units,
        acca_stake_units=tier.acca_stake_units,
    )
    sure_cfg = replace(SureSlipConfig.from_settings(), stake_units=tier.sure_stake_units)
    slips = SlipGenerator(gen_cfg, sure_cfg).build_all(value_bets)
    if not slips:
        print("  No slips cleared the quality bar — stand down this session.")
        if tg.is_configured:
            tg.send(f"{session.emoji} {session.name}: no slips cleared the bar.")
        return

    bankroll = Bankroll()
    placed: list[Slip] = []
    bet_ids: dict[str, str] = {}
    for slip in slips:
        if not bankroll.can_place(slip.stake_units):
            print(f"  !! Risk control blocked {slip.slip_type} {slip.slip_id} (exposure).")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip, session=session.name)
        bet_ids[slip.slip_id] = bet_id
        placed.append(slip)
        print()
        print(slip.render())
        print(f"  -> logged as {bet_id} (PENDING, {session.name})")

    # ---------- sheets + Telegram ---------- #
    _banner("Platform pick sheets (console + Telegram)")
    for slip in placed:
        sheet: PlatformSheet | None = choose_platform(slip)
        bet_id = bet_ids[slip.slip_id]
        if sheet is not None:
            text = sheet.render(bet_id=bet_id)
            print(text)
            if tg.is_configured:
                tg.send(f"{session.emoji} {session.name}\n\n{text}")
        else:
            print(slip.render())
        print()

    # ---------- summary ---------- #
    _banner("Session summary")
    total_stake = sum(s.stake_units for s in placed)
    expected_profit = sum(s.stake_units * s.ev_per_unit for s in placed)
    for slip in placed:
        print(f"  {slip.summary_line()}")
    print(
        f"\n  Session {session.name} | Slips: {len(placed)} | "
        f"Stake: {total_stake:.2f}u | Expected P/L: {expected_profit:+.2f}u"
    )
    print(f"  Metrics: {logger.metrics()}")

    if tg.is_configured:
        lines = [f"📊 {session.emoji} {session.name} session summary ({source})"]
        lines += [f"• {s.summary_line()}" for s in placed]
        lines.append(f"Stake {total_stake:.2f}u | Expected P/L {expected_profit:+.2f}u")
        lines.append("Load each sheet on its named platform, register the code, "
                     "settle after the matches.")
        tg.send("\n".join(lines))


if __name__ == "__main__":
    main()