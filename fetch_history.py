"""
fetch_history.py - downloads historical results from OpenFootball (GitHub,
free, reliable) and builds data/history.csv for the strength engine.

    python fetch_history.py

Covers: Premier League, Championship, Bundesliga, La Liga, Serie A,
Ligue 1 - two recent seasons. Future fixtures (no score) are skipped.
Requires: nothing but the standard library. ~5,000 matches expected.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from pathlib import Path

OUT = Path("data") / "history.csv"

LEAGUES = {
    "en.1": "Premier League",
    "en.2": "EFL Championship",
    "de.1": "Bundesliga",
    "es.1": "La Liga",
    "it.1": "Serie A",
    "fr.1": "Ligue 1",
}
SEASONS = ["2024-25", "2023-24"]
BASE = ("https://raw.githubusercontent.com/openfootball/football.json/"
        "master/{season}/{league}.json")

rows: list[list] = []
skipped = 0

for season in SEASONS:
    for code, league_name in LEAGUES.items():
        url = BASE.format(season=season, league=code)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sporty-edge/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"  skip {season}/{code}: {type(exc).__name__}")
            continue

        matches = data.get("matches") or []
        got = 0
        for m in matches:
            s1, s2 = m.get("score1"), m.get("score2")
            if s1 is None or s2 is None:
                skipped += 1
                continue  # future fixture, no result yet
            rows.append([
                str(m.get("date") or ""),
                league_name,
                str(m.get("team1") or ""),
                str(m.get("team2") or ""),
                int(s1),
                int(s2),
            ])
            got += 1
        print(f"  {season} {league_name:<18} {got:>4} matches")

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["date", "league", "home_team", "away_team",
                "home_goals", "away_goals"])
    w.writerows(rows)

print(f"\nWROTE {len(rows)} matches -> {OUT}  (skipped {skipped} fixtures without scores)")
print("Next:  python -m models.strength_engine")