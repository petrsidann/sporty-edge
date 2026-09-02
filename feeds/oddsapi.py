"""
The Odds API feed — the automated data source.

Setup (one time):
    1. Register free at https://the-odds-api.com (500 free credits/month).
    2. Copy your API key.
    3. Either paste it into config/settings.py FeedSettings(odds_api_key=...)
       or set the environment variable ODDS_API_KEY (env var wins).

Credit budget (free tier = 500/month):
    Cost per fetch = 1 credit per region x 1 credit per market.
    Defaults: 1 region (eu), 1 market (h2h) -> 1 credit per sport per session.
    4 sports x 4 sessions/day x 30 days = 480 credits  -> fits the free tier.
    Set markets="h2h,totals" (doubles cost) only if you buy credits.

Model probabilities in feed mode = cross-book no-vig CONSENSUS:
    For each book: p_i = (1/odds_i) / overround   (proportional de-margin)
    Consensus = median of p_i across books.
    The comparator then flags any single book paying above consensus.
    These are real cross-platform inefficiencies; they are usually small.

CLI helpers (run from repo root):
    python3 -m feeds.oddsapi --list-sports   # discover valid sport keys
    python3 -m feeds.oddsapi --test          # one fetch, prints findings
"""

from __future__ import annotations

import json
import os
import statistics
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

from config.settings import FEED_SETTINGS
from odds.comparator import OddsQuote, Selection

_API_BASE = "https://api.the-odds-api.com/v4"
_TIMEOUT = 15

_H2H_NAME_TO_PICK = {"Draw": "Draw"}  # team names map positionally at parse time


class OddsApiFeed:
    """Fetches events + prices and builds consensus-priced candidates."""

    def __init__(
        self,
        api_key: str | None = None,
        sports: tuple[str, ...] | None = None,
        regions: str | None = None,
        markets: str | None = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("ODDS_API_KEY", "").strip()
            or FEED_SETTINGS.odds_api_key
        )
        self.sports = tuple(sports) if sports else FEED_SETTINGS.feed_sports
        self.regions = regions or FEED_SETTINGS.regions
        self.markets = markets or FEED_SETTINGS.markets

    # ------------------------------------------------------------------ #

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, str]) -> tuple[object, dict[str, str]]:
        """GET the API; returns (parsed_json, response_headers). Raises on HTTP errors."""
        query = urllib.parse.urlencode(params)
        url = f"{_API_BASE}{path}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "sporty-edge/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body), dict(resp.headers)

    # ------------------------------------------------------------------ #

    def list_sports(self) -> list[dict]:
        """All valid sport keys (costs 1 credit)."""
        data, _ = self._get("/sports", {"apiKey": self.api_key})
        return data  # type: ignore[return-value]

    def _fetch_sport(self, sport_key: str) -> list[dict] | None:
        try:
            data, headers = self._get(
                f"/sports/{sport_key}/odds",
                {
                    "apiKey": self.api_key,
                    "regions": self.regions,
                    "markets": self.markets,
                    "oddsFormat": "decimal",
                },
            )
            remaining = headers.get("x-requests-remaining", "?")
            print(f"    feed: {sport_key:<28} events={len(data):<4} credits left ~{remaining}")
            return data  # type: ignore[return-value]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read().decode("utf-8")).get("message", ""))
            except Exception:
                pass
            if exc.code == 401:
                print(f"    feed: {sport_key} -> 401 bad API key. Check ODDS_API_KEY.")
            elif exc.code == 429:
                print(f"    feed: {sport_key} -> 429 quota exhausted; falling back.")
            elif exc.code == 422:
                print(f"    feed: {sport_key} -> 422 invalid sport key (use --list-sports).")
            else:
                print(f"    feed: {sport_key} -> HTTP {exc.code} {detail}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"    feed: {sport_key} -> network error: {exc!r}")
            return None

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _devig(outcomes: dict[str, float]) -> dict[str, float] | None:
        """Proportional de-margin: p_i = (1/o_i) / sum(1/o). Needs >= 2 outcomes."""
        if len(outcomes) < 2:
            return None
        implied = {k: 1.0 / o for k, o in outcomes.items() if o > 1.0}
        total = sum(implied.values())
        if total <= 0:
            return None
        return {k: v / total for k, v in implied.items()}

    def _parse_event(self, event: dict) -> list[tuple[Selection, list[OddsQuote]]]:
        home = event.get("home_team") or "?"
        away = event.get("away_team") or "?"
        label = f"{home} vs {away}"
        league = event.get("sport_title") or ""
        match_id = str(event.get("id") or "")[:10].upper() or "UNKNOWN"

        per_book_h2h: dict[str, dict[str, float]] = {}
        totals_by_line: dict[float, dict[str, dict[str, float]]] = defaultdict(dict)

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

        candidates: list[tuple[Selection, list[OddsQuote]]] = []

        # ---- h2h consensus ---- #
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
        if n_books >= FEED_SETTINGS.min_books_for_consensus:
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
                if len(books) < FEED_SETTINGS.min_books_for_consensus:
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

        return candidates

    # ------------------------------------------------------------------ #

    def collect(self) -> list[tuple[Selection, list[OddsQuote]]]:
        """Fetch every configured sport and return consensus-priced candidates."""
        if not self.is_configured:
            return []
        all_candidates: list[tuple[Selection, list[OddsQuote]]] = []
        for sport_key in self.sports:
            events = self._fetch_sport(sport_key)
            if not events:
                continue
            for event in events:
                all_candidates.extend(self._parse_event(event))
        return all_candidates


if __name__ == "__main__":
    import sys

    feed = OddsApiFeed()
    if not feed.is_configured:
        print("No API key. Set ODDS_API_KEY or FeedSettings(odds_api_key=...).")
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
            print(f"  {sel.match_label:<34} {sel.market}->{sel.selection:<5} "
                  f"consensus {sel.model_probability:.1%} best {best:.2f}")