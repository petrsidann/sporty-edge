"""
The Odds API feed — automated data source with caching, key failover,
and an event-time window.

Event window (the "bet now, know tonight" feature):
    Every event from the API carries `commence_time`.  Events outside the
    window [min_hours_ahead, max_hours_ahead] are skipped BEFORE pricing,
    so the system only ever flags games that start soon and settle today.
    Defaults come from FeedSettings (0h..12h = "resolves today"); run_daily
    can override per run with  --hours N.  The filter applies at parse time
    on cached data, so changing the window costs ZERO credits.
    Every candidate's match label now ends with its kickoff in EAT
    (e.g. "· Wed 21:10 EAT"), and that time flows through to pick sheets.

Setup (one time):
    1. Register free at https://the-odds-api.com (500 free credits/month).
    2. Run python3 setup_credentials.py to store the key, or set ODDS_API_KEY.

Credit math WITH the cache:
    Each sport is fetched at most once per cache_ttl_hours (default 6h).
    1 credit per market per sport per refresh: h2h+totals+spreads = 3
    credits/sport/refresh (see FeedSettings for the full table).
    Force a refresh: delete data/feed_cache.json or pass --refresh.

Key failover:
    Keys are tried in order: $ODDS_API_KEY -> FeedSettings.odds_api_key ->
    each entry in FeedSettings.api_keys.  On 401/429 the next key is tried.

De-margining — power method:
    Books load extra margin onto longshots (favourite-longshot bias), so we
    solve for k with sum((1/odds_i)^k) = 1 and use p_i = q_i^k.  Model
    probabilities in feed mode = cross-book power-devig MEDIAN.

CLI helpers (repo root):
    python3 -m feeds.oddsapi --list-sports   # discover valid sport keys
    python3 -m feeds.oddsapi --test          # one fetch, prints findings
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import FEED_SETTINGS
from odds.comparator import OddsQuote, Selection

_API_BASE = "https://api.the-odds-api.com/v4"
_TIMEOUT = 15
_H2H_NAME_TO_PICK = {"Draw": "Draw"}
_CACHE_PATH = Path("data") / "feed_cache.json"

# Hardened reads: this module works even if config.settings.py is older.
_CFG_API_KEYS = tuple(getattr(FEED_SETTINGS, "api_keys", ()) or ())
_CFG_TTL_HOURS = float(getattr(FEED_SETTINGS, "cache_ttl_hours", 6.0))
_CFG_MIN_BOOKS = int(getattr(FEED_SETTINGS, "min_books_for_consensus", 2))
_CFG_SPORTS = tuple(getattr(FEED_SETTINGS, "feed_sports", ()) or ())
_CFG_MIN_HOURS = float(getattr(FEED_SETTINGS, "min_hours_ahead", 0.0))
_CFG_MAX_HOURS = float(getattr(FEED_SETTINGS, "max_hours_ahead", 12.0))

_EAT = timezone(timedelta(hours=3))  # Nairobi time, shown on every sheet


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO8601 timestamp ('Z' suffix tolerated); None on failure."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _kickoff_eat(ts: str) -> str:
    """Human kickoff time in EAT, e.g. 'Wed 21:10 EAT'."""
    dt = _parse_iso(ts)
    if dt is None:
        return "?"
    return dt.astimezone(_EAT).strftime("%a %H:%M EAT")


class OddsApiFeed:
    """Fetches events + prices (with cache + time window) and builds
    consensus-priced candidates."""

    def __init__(
        self,
        api_key: str | None = None,
        sports: tuple[str, ...] | None = None,
        regions: str | None = None,
        markets: str | None = None,
        min_hours_ahead: float | None = None,
        max_hours_ahead: float | None = None,
    ) -> None:
        keys = [os.environ.get("ODDS_API_KEY", "").strip()]
        keys.append((api_key or FEED_SETTINGS.odds_api_key or "").strip())
        keys.extend(str(k).strip() for k in _CFG_API_KEYS)
        seen: set[str] = set()
        self.api_keys: list[str] = [
            k for k in keys if k and not (k in seen or seen.add(k))
        ]
        self.sports = tuple(sports) if sports else _CFG_SPORTS
        self.regions = regions or FEED_SETTINGS.regions
        self.markets = markets or FEED_SETTINGS.effective_markets()
        self.min_hours_ahead = (
            _CFG_MIN_HOURS if min_hours_ahead is None else float(min_hours_ahead)
        )
        self.max_hours_ahead = (
            _CFG_MAX_HOURS if max_hours_ahead is None else float(max_hours_ahead)
        )

    # ------------------------------------------------------------------ #

    def is_configured(self) -> bool:
        return bool(self.api_keys)

    def sports_with_events_within(self, window_hours: float) -> tuple[str, ...]:
        """Sport keys with at least one cached event starting within the window.

        Reads the local cache only — costs ZERO credits.  Used by the CLV
        snapshot to refresh just the sports that actually have a match near
        kickoff instead of the whole slate.
        """
        cache = self._load_cache()
        now_dt = datetime.now(timezone.utc)
        hot: list[str] = []
        for sport, entry in cache.items():
            for event in entry.get("events", []):
                dt = _parse_iso(event.get("commence_time") or "")
                if dt is None:
                    continue
                hours_ahead = (dt - now_dt).total_seconds() / 3600.0
                if 0.0 <= hours_ahead <= window_hours:
                    hot.append(sport)
                    break
        return tuple(hot)

    def _get(self, path: str, params: dict[str, str]) -> tuple[object, dict[str, str]]:
        query = urllib.parse.urlencode(params)
        url = f"{_API_BASE}{path}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "sporty-edge/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body), dict(resp.headers)

    # ------------------------------------------------------------------ #

    def list_sports(self) -> list[dict]:
        """All valid sport keys (costs 1 credit)."""
        data, _ = self._get("/sports", {"apiKey": self.api_keys[0]})
        return data  # type: ignore[return-value]

    def _fetch_sport(self, sport_key: str) -> list[dict] | None:
        """Fetch one sport, failing over through the key list on 401/429."""
        for i, key in enumerate(self.api_keys):
            try:
                data, headers = self._get(
                    f"/sports/{sport_key}/odds",
                    {
                        "apiKey": key,
                        "regions": self.regions,
                        "markets": self.markets,
                        "oddsFormat": "decimal",
                    },
                )
                remaining = headers.get("x-requests-remaining", "?")
                label = f"key{i + 1}" if len(self.api_keys) > 1 else "key"
                print(
                    f"    feed: {sport_key:<34} events={len(data):<4} "
                    f"credits left ~{remaining} [{label}]"
                )
                return data  # type: ignore[return-value]
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = str(json.loads(exc.read().decode("utf-8")).get("message", ""))
                except Exception:
                    pass
                if exc.code in (401, 429) and i + 1 < len(self.api_keys):
                    print(
                        f"    feed: {sport_key} -> HTTP {exc.code} on key{i + 1}; "
                        f"trying next key..."
                    )
                    continue
                if exc.code == 401:
                    print(f"    feed: {sport_key} -> 401 all keys bad/expired.")
                elif exc.code == 429:
                    print(f"    feed: {sport_key} -> 429 quota exhausted on all keys.")
                elif exc.code == 422:
                    print(
                        f"    feed: {sport_key} -> 422 invalid sport key "
                        f"(run --list-sports and swap keys in settings)."
                    )
                else:
                    print(f"    feed: {sport_key} -> HTTP {exc.code} {detail}")
                return None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"    feed: {sport_key} -> network error: {exc!r}")
                return None
        return None

    # ------------------------------------------------------------------ #
    # Cache
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_cache() -> dict[str, dict]:
        """Return {sport_key: {"events": [...], "ts": epoch_seconds}}."""
        if not _CACHE_PATH.exists():
            return {}
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        out: dict[str, dict] = {}
        for sport, entry in (data.get("sports") or {}).items():
            try:
                events = entry.get("events")
                ts = float(entry.get("ts", 0))
                if isinstance(events, list) and ts > 0:
                    out[str(sport)] = {"events": events, "ts": ts}
            except (TypeError, ValueError, AttributeError):
                continue
        return out

    @staticmethod
    def _save_cache(merged: dict[str, dict]) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps({"sports": merged}), encoding="utf-8")
        except OSError as exc:
            print(f"    cache: could not write ({exc!r}) — continuing uncached.")

    # ------------------------------------------------------------------ #
    # De-margining (power method)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _devig(outcomes: dict[str, float]) -> dict[str, float] | None:
        """Power-method de-margin: corrects favourite-longshot bias.

        Proportional method: p_i = q_i / sum(q).  Power method: find k with
        sum(q_i^k) = 1 (bisection) and use p_i = q_i^k (renormalised).
        Requires >= 2 valid outcomes.
        """
        if len(outcomes) < 2:
            return None
        q = {name: 1.0 / odds for name, odds in outcomes.items() if odds > 1.0}
        if len(q) < 2:
            return None

        raw_total = sum(q.values())
        if raw_total <= 1.0:
            if raw_total <= 0:
                return None
            return {name: v / raw_total for name, v in q.items()}

        lo, hi = 1.0, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if sum(v**mid for v in q.values()) > 1.0:
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2.0

        probs = {name: v**k for name, v in q.items()}
        total = sum(probs.values())
        if total <= 0:
            return None
        return {name: v / total for name, v in probs.items()}

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def _parse_event(self, event: dict) -> list[tuple[Selection, list[OddsQuote]]]:
        min_books = _CFG_MIN_BOOKS
        home = event.get("home_team") or "?"
        away = event.get("away_team") or "?"
        # Kickoff time rides in the label so tables, sheets, ledger and
        # Telegram all show WHEN this game starts, everywhere, for free.
        label = f"{home} vs {away} · {_kickoff_eat(event.get('commence_time', ''))}"
        league = event.get("sport_title") or ""
        match_id = str(event.get("id") or "")[:10].upper() or "UNKNOWN"

        per_book_h2h: dict[str, dict[str, float]] = {}
        totals_by_line: dict[float, dict[str, dict[str, float]]] = defaultdict(dict)
        spreads_by_line: dict[float, dict[str, dict[str, float]]] = defaultdict(dict)

        for book in event.get("bookmakers") or []:
            book_title = book.get("title") or book.get("key") or "?"
            for market in book.get("markets") or []:
                mkey = market.get("key")
                if mkey == "h2h":
                    odds_map: dict[str, float] = {}
                    for out in market.get("outcomes") or []:
                        name = out.get("name") or ""
                        price = float(out.get("price") or 0.0)
                        if name == home:
                            odds_map["Home"] = price
                        elif name == away:
                            odds_map["Away"] = price
                        elif name in _H2H_NAME_TO_PICK:
                            odds_map["Draw"] = price
                    if odds_map:
                        per_book_h2h[book_title] = odds_map
                elif mkey == "totals":
                    points = market.get("points")
                    if points is None:
                        continue
                    for out in market.get("outcomes") or []:
                        side = out.get("name")  # "Over" | "Under"
                        price = float(out.get("price") or 0.0)
                        if side in ("Over", "Under") and price > 1.0:
                            totals_by_line[float(points)].setdefault(book_title, {})[side] = price
                elif mkey == "spreads":
                    # 2-way handicap market: the line lives in each outcome's
                    # "point" field (home -1.5 / away +1.5 are the same line).
                    for out in market.get("outcomes") or []:
                        name = out.get("name") or ""
                        point = out.get("point")
                        price = float(out.get("price") or 0.0)
                        if point is None or price <= 1.0:
                            continue
                        if name == home:
                            pick = "Home"
                        elif name == away:
                            pick = "Away"
                        else:
                            continue
                        line = round(abs(float(point)), 2)
                        spreads_by_line[line].setdefault(book_title, {})[pick] = price

        candidates: list[tuple[Selection, list[OddsQuote]]] = []

        # ---- h2h consensus (median of power-devig probabilities) ---- #
        devigged: dict[str, list[float]] = defaultdict(list)
        raw_quotes: dict[str, list[OddsQuote]] = defaultdict(list)
        for book_title, odds_map in per_book_h2h.items():
            dv = self._devig(odds_map)
            if dv is None:
                continue
            for pick, p in dv.items():
                devigged[pick].append(p)
            for pick, o in odds_map.items():
                if o > 1.0:
                    raw_quotes[pick].append(OddsQuote(book=book_title, decimal_odds=o))

        n_books = len(per_book_h2h)
        if n_books >= min_books:
            market = "1X2" if "Draw" in devigged else "ML"
            for pick, probs in devigged.items():
                consensus = float(statistics.median(probs))
                if not 0.02 <= consensus <= 0.98:
                    continue
                quotes = raw_quotes.get(pick, [])
                if not quotes:
                    continue
                candidates.append(
                    (
                        Selection(
                            match_id=match_id,
                            match_label=label,
                            league=league,
                            market=market,
                            selection=pick,
                            model_probability=consensus,
                        ),
                        quotes,
                    )
                )

        # ---- totals consensus (one or two most-common lines per event) ---- #
        if "totals" in self.markets:
            common_lines = [ln for ln, _ in Counter(
                {ln: len(books) for ln, books in totals_by_line.items()}
            ).most_common(2)]
            for line in common_lines:
                books = totals_by_line.get(line, {})
                if len(books) < min_books:
                    continue
                over_ps: list[float] = []
                over_quotes: list[OddsQuote] = []
                under_quotes: list[OddsQuote] = []
                for book_title, sides in books.items():
                    dv = self._devig(sides)
                    if dv is None or "Over" not in dv:
                        continue
                    over_ps.append(dv["Over"])
                    if "Over" in sides:
                        over_quotes.append(OddsQuote(book=book_title, decimal_odds=sides["Over"]))
                    if "Under" in sides:
                        under_quotes.append(OddsQuote(book=book_title, decimal_odds=sides["Under"]))
                if not over_ps:
                    continue
                consensus_over = float(statistics.median(over_ps))
                market = f"O/U {line:g}"
                for pick, p, quotes in (
                    ("Over", consensus_over, over_quotes),
                    ("Under", 1.0 - consensus_over, under_quotes),
                ):
                    if 0.02 <= p <= 0.98 and quotes:
                        candidates.append(
                            (
                                Selection(
                                    match_id=match_id, match_label=label,
                                    league=league, market=market,
                                    selection=pick, model_probability=p,
                                ),
                                quotes,
                            )
                        )

        # ---- spreads consensus (per line; 2-way Home/Away) --------------- #
        if "spreads" in self.markets:
            for line, books in spreads_by_line.items():
                if len(books) < min_books:
                    continue
                home_ps: list[float] = []
                home_quotes: list[OddsQuote] = []
                away_quotes: list[OddsQuote] = []
                for book_title, sides in books.items():
                    dv = self._devig(sides)
                    if dv is None or "Home" not in dv:
                        continue
                    home_ps.append(dv["Home"])
                    if "Home" in sides:
                        home_quotes.append(OddsQuote(book=book_title, decimal_odds=sides["Home"]))
                    if "Away" in sides:
                        away_quotes.append(OddsQuote(book=book_title, decimal_odds=sides["Away"]))
                if not home_ps:
                    continue
                consensus_home = float(statistics.median(home_ps))
                market = f"SPREAD {line:g}"
                for pick, p, quotes in (
                    ("Home", consensus_home, home_quotes),
                    ("Away", 1.0 - consensus_home, away_quotes),
                ):
                    if 0.02 <= p <= 0.98 and quotes:
                        candidates.append(
                            (
                                Selection(
                                    match_id=match_id, match_label=label,
                                    league=league, market=market,
                                    selection=pick, model_probability=p,
                                ),
                                quotes,
                            )
                        )

        return candidates

    # ------------------------------------------------------------------ #

    def collect(self, refresh: bool = False) -> list[tuple[Selection, list[OddsQuote]]]:
        """Fetch (or reuse cached) events, apply the time window, price.

        Window rule: an event is kept only if it starts within
        [min_hours_ahead, max_hours_ahead] from now.  Applies to cached
        data too, so changing the window mid-day costs zero credits.
        Pass --refresh (or refresh=True) to bypass the cache for one run —
        that costs 1 credit per market per sport (see FeedSettings).
        """
        if not self.api_keys:
            return []
        force = refresh or "--refresh" in sys.argv
        cache = {} if force else self._load_cache()
        ttl = _CFG_TTL_HOURS
        now = time.time()

        merged: dict[str, dict] = {}
        for sport in self.sports:
            entry = cache.get(sport)
            if entry is not None and (now - entry["ts"]) / 3600.0 < ttl:
                age = (now - entry["ts"]) / 3600.0
                print(
                    f"    cache: {sport:<34} {len(entry['events'])} events, "
                    f"age {age:.1f}h  [0 credits]"
                )
                merged[sport] = entry
            else:
                events = self._fetch_sport(sport)
                if events is not None:
                    merged[sport] = {"events": events, "ts": now}

        if merged:
            self._save_cache(merged)
        if not merged:
            stale = self._load_cache()
            if stale:
                print("    !! all fetches failed - using STALE cached odds; verify prices on your app before staking.")
                merged = stale

        now_dt = datetime.now(timezone.utc)
        total_events = 0
        kept_events = 0
        candidates: list[tuple[Selection, list[OddsQuote]]] = []

        for sport in self.sports:
            for event in merged.get(sport, {}).get("events", []):
                total_events += 1
                dt = _parse_iso(event.get("commence_time") or "")
                if dt is not None:
                    hours_ahead = (dt - now_dt).total_seconds() / 3600.0
                    if (
                        hours_ahead < self.min_hours_ahead
                        or hours_ahead > self.max_hours_ahead
                    ):
                        continue  # outside the window: not bettable-today
                kept_events += 1
                candidates.extend(self._parse_event(event))

        print(
            f"  Window: events starting {self.min_hours_ahead:.0f}-"
            f"{self.max_hours_ahead:.0f}h from now — kept {kept_events}/"
            f"{total_events} events."
        )
        return candidates


if __name__ == "__main__":
    feed = OddsApiFeed()
    if not feed.is_configured:
        print("No API key. Run python3 setup_credentials.py or set ODDS_API_KEY.")
        print("Free key: https://the-odds-api.com")
        sys.exit(1)
    if "--list-sports" in sys.argv:
        for s in feed.list_sports():
            print(f"  {s['key']:<34} {s['title']}")
    elif "--test" in sys.argv:
        cands = feed.collect()
        print(f"candidates: {len(cands)}")
        for sel, quotes in cands[:10]:
            best = max(q.decimal_odds for q in quotes)
            print(f"  {sel.match_label:<48} {sel.market}->{sel.selection:<5} "
                  f"consensus {sel.model_probability:.1%} best {best:.2f}")