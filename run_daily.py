"""
run_daily.py — the session runner.

One betting session:
    1. Detect (or take --session) the current session.
    2. Pull events + prices from The Odds API feed (cached, 6h TTL);
       fall back to your CSV files if the feed is unavailable.
    3. EV scan with a STALE-LINE GUARD: any candidate whose best price is
       more than SUSPECT_RATIO above consensus fair odds is excluded —
       divergences that large are broken feed lines, not real edges
       (the sharp books ARE the market).
    4. Dedupe guard: no slip is built containing a pick already PENDING
       in the ledger (protects manual re-runs and scheduled runs that
       land inside the cache TTL).
    5. Slip tiers: SURESLIP -> singles -> accas -> SPEC; if no edge is
       found, ACTION mode emits closest-to-value picks.
    6. Platform pick sheets -> console + Telegram, session-tagged.

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
from slips.action import build_action_picks
from slips.generator import Slip, SlipGenerator, SlipGeneratorConfig, SureSlipConfig
from slips.platform_slip import PlatformSheet, choose_platform
from slips.spec import build_spec_singles
from utils.aggression import current_tier
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import Session, by_name, detect

# A best price more than 20% above consensus fair odds is almost always a
# stale or wrong feed line rather than a real edge.  Genuine cross-book
# edges rarely exceed ~10-15%; artifacts diverge by 30-70%.
SUSPECT_RATIO: float = 1.20


def _banner(title: str) -> None:
    line = "=" * 74
    print(f"\n{line}\n  {title}\n{line}")


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_table(opportunities: list[BetOpportunity], limit: int = 12) -> None:
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
        print(f"  … and {len(opportunities) - limit} more.")


def _from_feed() -> list | None:
    """Try the automated feed; returns candidates or None when unavailable."""
    feed = OddsApiFeed()
    if not feed.is_configured:
        print("  Feed: no API key configured (ODDS_API_KEY / FeedSettings).")
        return None
    print(f"  Feed: fetching {len(feed.sports)} sport(s) ...")
    candidates = feed.collect()
    print(f"  Feed: {len(candidates)} priced selections from consensus.")
    return candidates


def _is_suspect(opp: BetOpportunity) -> bool:
    """True when the best price diverges too far above consensus fair odds."""
    fair = 1.0 / opp.selection.model_probability
    return opp.decimal_odds / fair > SUSPECT_RATIO


def _pending_leg_keys(logger: BetLogger) -> set[tuple[str, str, str]]:
    """All (match_id, market, selection) keys currently PENDING in the ledger."""
    keys: set[tuple[str, str, str]] = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add(
                (
                    str(leg.get("match_id") or ""),
                    str(leg.get("market") or ""),
                    str(leg.get("selection") or ""),
                )
            )
    return keys


def _dedupe(slips: list[Slip], pending_keys: set[tuple[str, str, str]]) -> list[Slip]:
    """Drop any slip containing a pick that is already PENDING."""
    kept: list[Slip] = []
    for slip in slips:
        keys = {(l.match_id, l.market, l.selection) for l in slip.legs}
        if keys & pending_keys:
            print(
                f"  .. dedupe: skipped {slip.slip_type} — pick already PENDING "
                f"in the ledger."
            )
            continue
        kept.append(slip)
    return kept


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

    # ---------- evaluate every candidate ---------- #
    _banner("Positive-EV scan")
    if source == "FEED":
        min_edge, min_ev = FEED_SETTINGS.min_edge_feed, FEED_SETTINGS.min_ev_feed
        print("  (feed mode: probabilities = cross-book no-vig consensus, "
              "power-method de-margin)")
    else:
        min_edge, min_ev = RISK_SETTINGS.min_edge, RISK_SETTINGS.min_ev_per_unit

    comparator = OddsComparator(min_edge=min_edge, min_ev_per_unit=min_ev)
    all_opportunities: list[BetOpportunity] = [
        comparator.evaluate(sel, quotes) for sel, quotes in candidates
    ]

    clean = [o for o in all_opportunities if not _is_suspect(o)]
    suspects = [o for o in all_opportunities if _is_suspect(o)]
    if suspects:
        print(
            f"  Stale-line guard: {len(suspects)} candidate(s) excluded "
            f"(best price >{SUSPECT_RATIO:.0%} above consensus fair odds — "
            f"broken feed lines, not edges)."
        )

    value_bets = comparator.rank(
        [o for o in clean if comparator.is_value(o)]
    )
    print(
        f"  Positive-EV opportunities: {len(value_bets)} "
        f"(min edge {min_edge:.1%}, min EV {min_ev:.1%})"
    )

    mode = "VALUE" if value_bets else "ACTION"
    slips: list[Slip]

    if value_bets:
        _print_table(value_bets)

        # ---------- value tiers: SURESLIP -> singles -> accas -> SPEC ---- #
        _banner("Slip construction (stake sizes from your aggression tier)")
        gen_cfg = replace(
            SlipGeneratorConfig.from_settings(),
            single_stake_units=tier.single_stake_units,
            acca_stake_units=tier.acca_stake_units,
        )
        sure_cfg = replace(
            SureSlipConfig.from_settings(), stake_units=tier.sure_stake_units
        )
        base_slips = SlipGenerator(gen_cfg, sure_cfg).build_all(value_bets)
        used_matches = {leg.match_id for s in base_slips for leg in s.legs}
        spec_slips = build_spec_singles(value_bets, exclude_match_ids=used_matches)
        slips = base_slips + spec_slips
    else:
        # ---------- ACTION mode: no edge, still deliver picks ----------- #
        print("  No measured edge — switching to ACTION mode (honest picks).")
        _print_table(sorted(clean, key=lambda o: o.edge, reverse=True), limit=10)
        _banner("ACTION mode — closest-to-value picks, fixed stake")
        print(
            "  These picks carry NO measured edge.  They are the candidates "
            "closest to breakeven right now, labeled ACTION in the ledger so "
            "their ROI is tracked separately."
        )
        slips = build_action_picks(clean)

    # ---------- dedupe against pending ledger ---------- #
    slips = _dedupe(slips, _pending_leg_keys(logger))
    if not slips:
        print(
            "  All candidate picks are already PENDING in the ledger — "
            "nothing new this session (dedupe guard)."
        )
        if tg.is_configured:
            tg.send(
                f"{session.emoji} {session.name}: picks already pending — "
                f"nothing new (dedupe guard)."
            )
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
                if slip.slip_type == "ACTION":
                    header = (
                        "🎲 ACTION pick — no edge detected; ranked closest to "
                        "value. Fixed stake."
                    )
                elif slip.slip_type == "SPEC":
                    header = "⚠ SPEC pick — quarter-stake outlier play."
                else:
                    header = f"{session.emoji} {session.name}"
                tg.send(f"{header}\n\n{text}")
        else:
            print(slip.render())
        print()

    # ---------- summary ---------- #
    _banner(f"Session summary ({mode} mode)")
    total_stake = sum(s.stake_units for s in placed)
    expected_profit = sum(s.stake_units * s.ev_per_unit for s in placed)
    for slip in placed:
        print(f"  {slip.summary_line()}")
    print(
        f"\n  Session {session.name} [{mode}] | Slips: {len(placed)} | "
        f"Stake: {total_stake:.2f}u | Expected P/L: {expected_profit:+.2f}u"
    )
    print(f"  Metrics: {logger.metrics()}")

    if tg.is_configured:
        lines = [f"📊 {session.emoji} {session.name} summary ({mode}, {source})"]
        lines += [f"• {s.summary_line()}" for s in placed]
        lines.append(f"Stake {total_stake:.2f}u | Expected P/L {expected_profit:+.2f}u")
        lines.append("Load each sheet on its named platform, register the code, "
                     "settle after the matches.")
        tg.send("\n".join(lines))


if __name__ == "__main__":
    main()