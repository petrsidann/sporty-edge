"""
smart_picks.py (ROLLING WINDOW + HIT-RATE lane) - run at ANY time, get 8
bets that all settle within the NEXT 4 HOURS. That is the whole rule.

    python run_now.py

    * Window = [now, now+4h]. Every pick: kickoff inside it AND
      kickoff + sport duration <= window end. Nothing from tomorrow, ever.
    * Lanes:
        [HIT]   - the high-win-rate lane (HIT_RATE_MODE in settings):
                  only picks with probability >= 0.80 from Over 0.5/1.5 and
                  Under 4.5/5.5 totals, moneylines with consensus >= 0.80,
                  and double chance; taken odds capped at 1.60.  Every [HIT]
                  pick still prints its TRUE probability -- a high hit rate
                  is the goal, not a profit guarantee.
        WINNER  - clear favorite -> pick the winner.
        TOTALS  - tight/draw-risky match -> pivot to the goals market;
                  scoring ignores the coin flip.
    * [MODEL] - when BOTH teams exist in data/history.csv
      (models/strength_engine.py) the probability is blended
      0.4 * model + 0.6 * consensus. Unknown teams = pure consensus.
    * 8 picks across the 5 bet-target apps (BET_TARGET_PLATFORMS):
      the 3 best-performing platforms (per the ledger) get 2 picks, the
      other 2 get 1.  With no platform history the default order stands.
      Stakes graded (HOT 1.0x / WARM 0.5x / STEADY 0.35x / SPICY 0.15x).
    * Dedupe: one bet per match, never duplicates, never both sides.
      Run again any time - only NEW picks are added (re-checked under the
      ledger lock at log time, so scheduler + manual runs cannot double-log).
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import (
    BET_TARGET_PLATFORMS,
    CALIBRATION_SETTINGS,
    HIT_RATE_MODE,
    HIT_RATE_SETTINGS,
    MODEL_BLEND_MODEL_WEIGHT,
)
from feeds.oddsapi import OddsApiFeed
from models.probability_engine import PoissonMatchModel
from models.strength_engine import StrengthEngine, load_engine
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator, OddsQuote
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, clock_line, detect
from utils.term import force_utf8_stdio

N_PICKS = 8
WINDOW_HOURS = 4.0
CACHE = Path("data") / "feed_cache.json"
#: Minimum settled bets recorded for a platform before its ledger performance
#: overrides the default rotation order.
MIN_PLATFORM_SAMPLE = 10
#: Double-chance reference prices are DERIVED from the 1X2 consensus (the
#: feed carries no DC quotes).  A margin haircut keeps the derived reference
#: conservative versus what books actually charge for DC.
DC_DERIVED_MARGIN = 0.05
DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6), ("mma", 1.5), ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for key, hours in DURATIONS:
        if key in s:
            return hours
    return 2.05


def _teams(label: str) -> tuple[str, str]:
    left, _, right = label.partition(" vs ")
    return left.strip(), right.split(" · ")[0].strip()


def _anchor(market: str, selection: str, label: str) -> str:
    home, away = _teams(label)
    m = market.upper()
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        if selection == "Home":
            return f"{home} to win"
        if selection == "Away":
            return f"{away} to win"
        return "Draw"
    if m.startswith("O/U"):
        return f"{selection} {market.replace('O/U ', '')} goals"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


def _pending(logger: BetLogger) -> tuple[set, set]:
    keys: set = set()
    matches: set = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"),
                      leg.get("selection")))
            matches.add(leg.get("match_id"))
    return keys, matches


def _kickoff_map() -> dict:
    out: dict = {}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            eid = str(ev.get("id") or "")[:10].upper()
            if eid and ev.get("commence_time"):
                out[eid] = str(ev["commence_time"])
    return out


def _kickoff_eat(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(EAT).strftime("%a %H:%M EAT")
    except (ValueError, TypeError):
        return "?"


def _kickoff_dt(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Calibration shrinkage + strength-model blending
# --------------------------------------------------------------------------- #

def _shrink_prob(prob: float) -> float:
    """Apply the calibration shrinkage factor (1.0 = no-op, default)."""
    factor = CALIBRATION_SETTINGS.shrinkage_factor
    if factor >= 1.0:
        return prob
    p = 0.5 + (prob - 0.5) * factor
    return min(max(p, 0.01), 0.99)


def _with_prob(opp: BetOpportunity, prob: float) -> BetOpportunity:
    """Copy of ``opp`` with a new model probability (dataclasses are frozen)."""
    return replace(opp, selection=replace(opp.selection, model_probability=prob))


def _blend_with_model(
    engine: StrengthEngine | None, opp: BetOpportunity
) -> tuple[BetOpportunity, bool]:
    """Blend consensus with the strength-engine model when BOTH teams are
    known: p = 0.4 * model + 0.6 * consensus (MODEL_BLEND_MODEL_WEIGHT).

    Returns (opportunity, blended_flag).  Unknown engine, unknown teams,
    unmodelled markets and any failure all fall back to pure consensus --
    blending is an upgrade, never a dependency.
    """
    if engine is None:
        return opp, False
    market = opp.selection.market.upper()
    pick = opp.selection.selection
    try:
        if market not in ("1X2", "ML") and not market.startswith(("O/U", "DC")):
            return opp, False
        home, away = _teams(opp.selection.match_label)
        if not engine.has(home, away):
            return opp, False
        xg = engine.expected_goals(home, away, opp.selection.league)
        model = PoissonMatchModel(xg)
        model_prob: float | None = None
        if market in ("1X2", "ML"):
            model_prob = model.one_x_two().get(pick)
        elif market.startswith("O/U"):
            line = float(market.split()[-1])
            model_prob = model.over_under(line).get(pick.lower())
        elif market.startswith("DC"):
            model_prob = model.double_chance().get(pick)
        if model_prob is None or not 0.0 < model_prob < 1.0:
            return opp, False
        blended = (MODEL_BLEND_MODEL_WEIGHT * model_prob
                   + (1.0 - MODEL_BLEND_MODEL_WEIGHT)
                   * opp.selection.model_probability)
        if not 0.01 <= blended <= 0.99:
            return opp, False
        return _with_prob(opp, blended), True
    except Exception:
        # The model must never cost a session: consensus stands.
        return opp, False


# --------------------------------------------------------------------------- #
# [HIT] lane
# --------------------------------------------------------------------------- #

def _dc_candidates(h2h: dict) -> list[tuple[BetOpportunity, str]]:
    """Double-chance candidates DERIVED from the 1X2 consensus.

    The feed carries no DC quotes, so the reference price is derived from
    the devigged consensus: ref = 1/p * (1 - DC_DERIVED_MARGIN), then the
    usual 3% price floor applies on top.  This is disclosed on the pick
    message so the owner verifies the real DC price on the app.
    """
    out: list[tuple[BetOpportunity, str]] = []
    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    for sides in h2h.values():
        h, d, a = (sides.get("Home"), sides.get("Draw"), sides.get("Away"))
        if not (h and d and a):
            continue  # 2-way sport: no double chance exists
        for pick, opps in (("1X", (h, d)), ("X2", (d, a)), ("12", (h, a))):
            p = sum(o.selection.model_probability for o in opps)
            if not (HIT_RATE_SETTINGS.min_prob - 1e-9 <= p <= 0.995):
                continue
            derived = (1.0 / p) * (1.0 - DC_DERIVED_MARGIN)
            if derived > HIT_RATE_SETTINGS.max_odds + 1e-9:
                continue
            sel = replace(opps[0].selection, market="DC", selection=pick,
                          model_probability=p)
            out.append((comparator.evaluate(
                sel, [OddsQuote(book="derived-1X2", decimal_odds=round(derived, 3))]
            ), "HIT"))
    return out


def _hit_candidates(h2h: dict, totals: dict) -> list[tuple[BetOpportunity, str]]:
    """All [HIT]-eligible candidates: probability >= min_prob, odds <= cap.

    Sources: moneylines with a strong consensus, the low/high totals lines
    (Over 0.5 / Over 1.5 / Under 4.5 / Under 5.5 by default), and derived
    double chance.  Ranking and per-match dedupe happen in main().
    Thresholds tolerate a 1e-9 float wobble (0.70 + 0.10 < 0.80 in binary).
    """
    out: list[tuple[BetOpportunity, str]] = []
    p_min = HIT_RATE_SETTINGS.min_prob - 1e-9
    o_max = HIT_RATE_SETTINGS.max_odds + 1e-9

    for sides in h2h.values():
        for opp in sides.values():
            if opp.selection.model_probability >= p_min \
                    and opp.decimal_odds <= o_max:
                out.append((opp, "HIT"))

    hit_lines = set(HIT_RATE_SETTINGS.lines)
    for lines in totals.values():
        for opp in lines:
            market = opp.selection.market.upper()
            try:
                line = float(market.split()[-1])
            except (ValueError, IndexError):
                continue
            if line in hit_lines and opp.selection.model_probability >= p_min \
                    and opp.decimal_odds <= o_max:
                out.append((opp, "HIT"))

    out.extend(_dc_candidates(h2h))
    return out


# --------------------------------------------------------------------------- #
# Platform rotation (3 platforms x 2 picks + 2 x 1, ranked by the ledger)
# --------------------------------------------------------------------------- #

def platform_slots(
    performance: dict[str, dict],
    platforms: list[str],
    n_slots: int = N_PICKS,
    min_sample: int = MIN_PLATFORM_SAMPLE,
) -> list[str]:
    """The ``n_slots`` platform assignments for one session.

    ``performance`` maps platform -> {"n", "wins", "staked", "profit"} from
    the ledger.  Platforms with >= min_sample settled bets are ranked by
    win rate (then ROI -- the owner's goal is the hit rate, profit breaks
    ties); the top 3 get two picks, the rest get one.  Platforms without
    enough history keep the default order.  Pure function: testable
    without a feed or a ledger.
    """
    targets = list(platforms)
    if not targets:
        return []
    measured = []
    for name in targets:
        stats = performance.get(name) or {}
        settled = int(stats.get("n") or 0)
        if settled >= min_sample and stats.get("staked"):
            wins = int(stats.get("wins") or 0)
            roi = float(stats.get("profit") or 0.0) / float(stats["staked"])
            measured.append((name, wins / settled, roi))
    ranked = [name for name, _wr, _roi in
              sorted(measured, key=lambda t: (t[1], t[2]), reverse=True)]
    ordered = ranked + [p for p in targets if p not in ranked]

    slots: list[str] = []
    doubles = 3 if len(ordered) >= 5 else len(ordered)
    while len(slots) < n_slots:
        for i, name in enumerate(ordered):
            if len(slots) >= n_slots:
                break
            slots.append(name)
            if i >= doubles:
                continue
            if len(slots) < n_slots:
                slots.append(name)
    return slots


def _platform_performance(logger: BetLogger) -> dict[str, dict]:
    """Per-platform settled record from the ledger (platform recorded at
    log time by smart_picks, or via attach_code)."""
    perf: dict[str, dict] = {}
    for rec in logger._read_all():
        plat = rec.get("platform")
        status = rec.get("status")
        if not plat or status not in ("WIN", "LOSS"):
            continue
        d = perf.setdefault(str(plat), {"n": 0, "wins": 0,
                                        "staked": 0.0, "profit": 0.0})
        d["n"] += 1
        d["staked"] += float(rec.get("stake_units") or 0.0)
        d["profit"] += float(rec.get("profit_units") or 0.0)
        if status == "WIN":
            d["wins"] += 1
    return perf


def _tier(prob: float) -> tuple[str, float]:
    if prob >= 0.75:
        return "HOT", 1.00
    if prob >= 0.65:
        return "WARM", 0.50
    if prob >= 0.55:
        return "STEADY", 0.35
    return "SPICY", 0.15


def main() -> None:
    force_utf8_stdio()
    base_stake = 0.5
    if "--stake" in sys.argv:
        i = sys.argv.index("--stake")
        try:
            base_stake = float(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass

    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=WINDOW_HOURS)

    print("=" * 70)
    print(f"  SMART PICKS | {session.emoji} {session.name} | "
          f"window: NOW -> +{WINDOW_HOURS:.0f}h")
    print(f"  {clock_line()}")
    print("=" * 70)

    engine = load_engine()
    if engine is None:
        print("  strength engine: data/history.csv not found -- "
              "pure consensus (export results to enable [MODEL] blending).")
    else:
        print(f"  strength engine: {len(engine.ratings_table)} team(s) rated "
              f"from data/history.csv -- blending "
              f"{MODEL_BLEND_MODEL_WEIGHT:.0%} model / "
              f"{1 - MODEL_BLEND_MODEL_WEIGHT:.0%} consensus where both teams "
              f"are known.")
    print(f"  HIT-RATE lane: {'ON' if HIT_RATE_MODE else 'off'} "
          f"(prob >= {HIT_RATE_SETTINGS.min_prob:.0%}, "
          f"odds <= {HIT_RATE_SETTINGS.max_odds:.2f})")

    cands = OddsApiFeed(min_hours_ahead=0.0,
                        max_hours_ahead=WINDOW_HOURS,
                        markets="h2h,totals").collect()
    if not cands:
        # distinguish dead window (cache fresh) from real feed failure
        cache_age_h = 99.0
        try:
            age = time.time() - CACHE.stat().st_mtime
            cache_age_h = age / 3600.0
        except OSError:
            pass
        if cache_age_h < 2.0:
            # fetch worked; the 4h window is simply empty (dead hours)
            ping = Path("data") / "last_empty_ping.txt"
            now_ts = time.time()
            last = 0.0
            try:
                last = float(ping.read_text().strip())
            except (OSError, ValueError):
                pass
            print("no games start in the next 4h (dead hours) - quiet until supply returns")
            if tg.is_configured and (now_ts - last) > 6 * 3600:
                tg.send("No games start in the next 4h window (global dead hours). "
                        "Next session resumes automatically.")
                ping.write_text(str(now_ts))
            return
        msg = "[FAIL] feed fetch failed (keys/credits?) - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending_keys, pending_matches = _pending(logger)
    kmap = _kickoff_map()

    h2h: dict[str, dict[str, BetOpportunity]] = defaultdict(dict)
    totals: dict[str, list[BetOpportunity]] = defaultdict(list)
    blended: set[tuple[str, str, str]] = set()
    supply = 0

    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        prob = _shrink_prob(opp.selection.model_probability)
        if not 0.05 <= prob <= 0.95:
            continue
        opp = _with_prob(opp, prob)
        ko = _kickoff_dt(kmap.get(opp.selection.match_id, ""))
        if ko is None or ko < now:
            continue
        dur = sport_duration(opp.selection.league)
        if (ko + timedelta(hours=dur)) > window_end:
            continue  # must settle within the 4h window
        supply += 1
        opp, was_blended = _blend_with_model(engine, opp)
        if was_blended:
            blended.add((opp.selection.match_id, opp.selection.market,
                         opp.selection.selection))
        if opp.selection.market.upper() in ("1X2", "ML"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif opp.selection.market.upper().startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    # The 3-way split needs a small fix: the totals-pivot (cloudy matches)
    # can consume a strong favourite before the HIT lane sees its ML/DC.
    # Check the HIT supply on match A before deciding.

    # ------------------------- lane 1: [HIT] picks ------------------------ #
    used_matches: set[str] = set()
    # Reserve matches with a strong favourite Side (Home/Away prob >= 0.80)
    # for the HIT lane before any match is offered to the totals pivot.
    reserved: set[str] = set()
    if HIT_RATE_MODE:
        for mid, sides in h2h.items():
            for side in ("Home", "Away"):
                o = sides.get(side)
                if o is not None and o.selection.model_probability >= 0.80:
                    reserved.add(mid)
                    break
    hit_picks: list[tuple[BetOpportunity, str]] = []
    if HIT_RATE_MODE:
        for opp, lane in _hit_candidates(h2h, totals):
            key = (opp.selection.match_id, opp.selection.market,
                   opp.selection.selection)
            if key in pending_keys or opp.selection.match_id in pending_matches:
                continue
            if opp.selection.match_id in used_matches:
                continue  # one bet per match, never both sides
            hit_picks.append((opp, lane))
            used_matches.add(opp.selection.match_id)
        hit_picks.sort(key=lambda t: t[0].selection.model_probability,
                       reverse=True)
        hit_picks = hit_picks[:N_PICKS]

    # ---------------- lane 2: WINNER/TOTALS for the remainder -------------- #
    candidates: list[tuple[BetOpportunity, str]] = []
    for mid, sides in h2h.items():
        if mid in (used_matches | reserved):
            continue  # the HIT lane already booked (or reserved) this match
        draw = sides.get("Draw")
        best = None
        for o in (sides.get("Home"), sides.get("Away")):
            if o and (best is None or
                      o.selection.model_probability >
                      best.selection.model_probability):
                best = o
        if best is None:
            continue
        cloudy = ((draw is not None and
                   draw.selection.model_probability >= 0.30) or
                  best.selection.model_probability < 0.50)
        if cloudy:
            lines = totals.get(mid, [])
            if lines:
                top = max(lines, key=lambda o: o.selection.model_probability)
                candidates.append((top, "TOTALS"))
        else:
            candidates.append((best, "WINNER"))
    for mid, lines in totals.items():
        if mid not in h2h and mid not in used_matches and lines:
            top = max(lines, key=lambda o: o.selection.model_probability)
            candidates.append((top, "TOTALS"))

    seen, fresh = set(), []
    for o, lane in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in pending_keys or o.selection.match_id in pending_matches or k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = hit_picks + fresh[:N_PICKS - len(hit_picks)]

    print(f"\n  Supply: {supply} markets in-window | "
          f"{len(hit_picks)} [HIT] + {len(fresh)} standard | "
          f"taking {len(picks)}\n")

    if not picks:
        msg = (f"{session.emoji} {session.name}: every in-window game is "
               f"already logged. Run again in the next window for fresh 8.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    slots = platform_slots(_platform_performance(logger),
                           BET_TARGET_PLATFORMS)
    placed: list[tuple[str, BetOpportunity, str, str, float, str]] = []

    for i, (opp, lane) in enumerate(picks, start=1):
        platform = slots[(i - 1) % len(slots)]
        prob = opp.selection.model_probability
        if lane == "HIT":
            label, mult = "HIT", HIT_RATE_SETTINGS.stake_mult
        else:
            label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            continue
        leg = slip.legs[0]
        key = (leg.match_id, leg.market, leg.selection)
        # Lock-guarded dedupe re-check: a scheduler run and a manual run can
        # no longer both log the same match between the scan and the write.
        bet_id = logger.try_log_slip(
            slip, session=session.name, platform=platform,
            extra={"lane": lane, "tags": _tags_for(opp, lane, blended).strip()},
            leg_keys={key}, match_ids={leg.match_id},
        )
        if bet_id is None:
            print(f"  skip {key} -- already pending (logged by another run).")
            continue
        bankroll.register_bet(eff)
        placed.append((platform, opp, bet_id, label, eff, lane))
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        tags = _tags_for(opp, lane, blended)
        derived = "derived-1X2" in leg.quotes_by_book
        msg = (f"{lane} {i}/{len(picks)} -> {platform.upper()} [{label}]{tags}\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done} (inside your 4h window)\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               + ("(DC price derived from the 1X2 consensus -- confirm the "
                  "double-chance price on the app)\n" if derived else "")
               + f"Stake {eff:.2f}u | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e, _ in placed)
        total = sum(e for _, _, _, _, e, _ in placed)
        n_hit = sum(1 for _, _, _, _, _, lane in placed if lane == "HIT")
        summary = (f"{len(placed)}/8 picks | {n_hit} [HIT]"
                   f"{' + ' + str(len(placed) - n_hit) + ' standard' if n_hit else ''}"
                   f" | stake {total:.2f}u | expected {exp:+.2f}u | ALL settle by "
                   f"{(now + timedelta(hours=WINDOW_HOURS)).astimezone(EAT).strftime('%H:%M EAT')}\n"
                   f"Next window: run again any time after these finish.")
        print(summary)
        if tg.is_configured:
            tg.send(summary)


def _tags_for(opp: BetOpportunity, lane: str,
              blended: set[tuple[str, str, str]]) -> str:
    """Telegram tags: [HIT] for the hit lane, [MODEL] when blended."""
    tags = ""
    if lane == "HIT":
        tags += " [HIT]"
    key = (opp.selection.match_id, opp.selection.market, opp.selection.selection)
    if key in blended:
        tags += " [MODEL]"
    return tags


if __name__ == "__main__":
    main()