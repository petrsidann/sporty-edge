"""
Closing Line Value (CLV) — the professional scorecard for a bettor.

The idea: a bettor who consistently beats the closing line (gets a better
price than the market's last word before kickoff) is making +EV decisions
in the long run, even before results settle.  CLV per leg:

    CLV = placed_odds / closing_best_odds - 1

A positive CLV means you got on BEFORE the price shortened; you "beat the
close".  Reported metrics:

    beat_close_rate : share of recorded legs with CLV > 0
    avg_clv         : mean CLV across recorded legs

Workflow (data/closing.jsonl, one record per leg):
    1. When a PENDING slip's event kicks off within the next hour, fetch
       fresh feed odds for that event's sport (feeds/oddsapi reuse).
    2. Store the best pre-close price for each leg together with the price
       actually placed.
    3. Each leg is recorded exactly once; re-running the snapshot never
       double-counts.

CREDIT COST (documented honestly):
    A snapshot refreshes ONLY the sports that have an event inside the
    closing window (found via the local cache, 0 credits) and then forces
    one fresh fetch per such sport.  With FeedSettings markets
    "h2h,totals" that is 2 credits per refreshed sport; with spreads added,
    3.  A typical day touches 1-3 near-kickoff sports -> 2-9 credits.
    If the cache is empty (fresh machine) the fallback refreshes the whole
    slate — that costs ~2 credits x N sports in one go.

Failure policy: CLV is instrumentation, never a pick source.  A missing
feed, an empty ledger, or an unpriceable leg degrades to "no record" with
a message and never raises into the session pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from feeds.oddsapi import OddsApiFeed
from odds.comparator import OddsQuote, Selection

CLOSING_PATH = Path("data") / "closing.jsonl"

#: How close to kickoff a price snapshot counts as "the closing line".
DEFAULT_CLOSING_WINDOW_HOURS: float = 1.0


def _closing_key(bet_id: str, match_id: str, market: str, selection: str) -> tuple:
    return (bet_id, match_id, market, selection)


def read_closing_records(path: str | Path = CLOSING_PATH) -> list[dict]:
    """All recorded CLV rows; tolerates a missing or corrupted file."""
    if not Path(path).exists():
        return []
    records: list[dict] = []
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        return []
    return records


def _append_records(records: list[dict], path: str | Path = CLOSING_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def closing_metrics(records: list[dict] | None = None) -> dict[str, float]:
    """beat_close_rate and avg_clv over all recorded legs (0.0 when empty)."""
    if records is None:
        records = read_closing_records(CLOSING_PATH)
    clvs = [
        float(r["clv"])
        for r in records
        if isinstance(r.get("clv"), (int, float))
    ]
    if not clvs:
        return {"clv_legs": 0, "beat_close_rate": 0.0, "avg_clv": 0.0}
    beats = sum(1 for c in clvs if c > 0.0)
    return {
        "clv_legs": len(clvs),
        "beat_close_rate": round(beats / len(clvs), 4),
        "avg_clv": round(sum(clvs) / len(clvs), 4),
    }


def _closing_price_map(
    candidates: list[tuple[Selection, list[OddsQuote]]],
) -> dict[tuple[str, str, str], float]:
    """(match_id, market, selection) -> best available pre-close price."""
    best: dict[tuple[str, str, str], float] = {}
    for selection, quotes in candidates:
        if not quotes:
            continue
        key = (selection.match_id, selection.market, selection.selection)
        price = max(q.decimal_odds for q in quotes)
        best[key] = max(best.get(key, 0.0), price)
    return best


def snapshot_closing(
    pending_records: list[dict],
    window_hours: float = DEFAULT_CLOSING_WINDOW_HOURS,
    path: str | Path = CLOSING_PATH,
    feed: OddsApiFeed | None = None,
) -> list[dict]:
    """Record pre-close prices for pending legs kicking off within the window.

    For every PENDING leg whose event starts within ``window_hours`` of now,
    fetches fresh feed prices and appends one CLV record per leg:

        {"bet_id", "match_id", "market", "selection", "placed_odds",
         "closing_odds", "clv", "snapped_at"}

    Legs already recorded, events outside the window, and markets the feed
    no longer lists are all skipped with a printed note.  Returns the new
    records (empty when there was nothing to do).
    """
    # ---- collect the legs that could still get a closing reference ---- #
    legs: list[tuple[dict, dict]] = []
    for rec in pending_records:
        if rec.get("status") != "PENDING":
            continue
        for leg in rec.get("legs", []):
            if isinstance(leg, dict) and leg.get("match_id"):
                legs.append((rec, leg))
    if not legs:
        return []

    # ---- which sports actually need a fresh fetch (cache scan, 0 credits) -- #
    base_feed = feed or OddsApiFeed()
    if not base_feed.is_configured:
        print("  CLV: no API key configured — closing snapshot skipped.")
        return []
    credits_per_sport = len(
        [m for m in base_feed.markets.split(",") if m.strip()]
    )
    hot_sports = base_feed.sports_with_events_within(window_hours)
    if hot_sports:
        print(
            f"  CLV: refreshing {len(hot_sports)} near-kickoff sport(s) "
            f"({', '.join(hot_sports)}) — ~{credits_per_sport * len(hot_sports)} "
            f"credits."
        )
    else:
        all_sports = base_feed.sports
        print(
            f"  CLV: cache empty — refreshing all {len(all_sports)} sports "
            f"once (~{credits_per_sport * len(all_sports)} credits)."
        )
    closing_feed = OddsApiFeed(
        sports=hot_sports or base_feed.sports,
        min_hours_ahead=0.0,
        max_hours_ahead=window_hours,
    )
    try:
        candidates = closing_feed.collect(refresh=True)
    except Exception as exc:  # never let CLV kill a session
        print(f"  CLV: feed refresh failed ({exc!r}) — snapshot skipped.")
        return []

    prices = _closing_price_map(candidates)
    already = {
        _closing_key(
            r.get("bet_id", ""), r.get("match_id", ""),
            r.get("market", ""), r.get("selection", ""),
        )
        for r in read_closing_records(path)
    }

    new_records: list[dict] = []
    for rec, leg in legs:
        key3 = (
            str(leg.get("match_id") or ""),
            str(leg.get("market") or ""),
            str(leg.get("selection") or ""),
        )
        if key3 not in prices:
            continue  # market dropped by the feed or event already started
        bet_id = str(rec.get("bet_id") or "")
        if _closing_key(bet_id, *key3) in already:
            continue  # one record per leg, ever
        placed = float(leg.get("decimal_odds") or 0.0)
        close = prices[key3]
        if placed <= 1.0 or close <= 1.0:
            continue
        clv = placed / close - 1.0
        new_records.append(
            {
                "bet_id": bet_id,
                "match_id": key3[0],
                "market": key3[1],
                "selection": key3[2],
                "placed_odds": round(placed, 4),
                "closing_odds": round(close, 4),
                "clv": round(clv, 6),
                "snapped_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        )

    if not new_records:
        print("  CLV: no new closing prices to record this run.")
        return []
    try:
        _append_records(new_records, path)
    except OSError as exc:
        print(f"  CLV: could not write {path} ({exc!r}) — records lost this run.")
        return []
    print(f"  CLV: recorded {len(new_records)} closing leg(s) -> {path}")
    return new_records


def clv_summary_line(metrics: dict[str, float]) -> str:
    """One human-readable line for the session summary / Telegram."""
    if not metrics.get("clv_legs"):
        return "CLV: no closing references recorded yet."
    return (
        f"CLV: beat close {metrics['beat_close_rate'] * 100:.0f}% of "
        f"{metrics['clv_legs']} leg(s) | avg CLV {metrics['avg_clv'] * 100:+.2f}%"
    )
