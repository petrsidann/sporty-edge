"""
fetch_history.py v2 - builds data/history.csv from TWO sources:
  1. OpenFootball JSON (handles both score formats, 3 seasons)
  2. football-data.co.uk direct CSV endpoints (fallback if #1 is thin)
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path

OUT = Path("data") / "history.csv"
rows: list[list] = []


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sporty-edge"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------- source 1: openfootball ----------
OF_LEAGUES = {
    "en.1": "Premier League", "de.1": "Bundesliga", "es.1": "La Liga",
    "it.1": "Serie A", "fr.1": "Ligue 1", "en.2": "EFL Championship",
}
OF_SEASONS = ["2024-25", "2023-24", "2022-23"]

for season in OF_SEASONS:
    for code, league_name in OF_LEAGUES.items():
        url = (f"https://raw.githubusercontent.com/openfootball/football.json/"
               f"master/{season}/{code}.json")
        try:
            data = json.loads(_get(url).decode("utf-8"))
        except Exception:
            continue
        got = 0
        for m in data.get("matches") or []:
            s1, s2 = m.get("score1"), m.get("score2")
            if s1 is None or s2 is None:
                sc = m.get("score") or {}
                ft = sc.get("ft") if isinstance(sc, dict) else None
                if isinstance(ft, list) and len(ft) == 2:
                    s1, s2 = ft[0], ft[1]
            if s1 is None or s2 is None:
                continue
            rows.append([str(m.get("date") or ""), league_name,
                         str(m.get("team1") or ""), str(m.get("team2") or ""),
                         int(s1), int(s2)])
            got += 1
        print(f"  openfootball {season} {league_name:<18} {got:>4}")

print(f"  openfootball total so far: {len(rows)}")

# ---------- source 2: football-data.co.uk direct CSVs (fallback) ----------
if len(rows) < 1000:
    print("  openfootball thin -> trying football-data.co.uk direct CSVs...")
    FD_LEAGUES = {
        "E0": "Premier League", "SP1": "La Liga", "I1": "Serie A",
        "D1": "Bundesliga", "F1": "Ligue 1", "N1": "Eredivisie",
        "P1": "Primeira Liga", "SC0": "Scotland Prem", "B1": "Belgium First Div",
    }
    FD_SEASONS = ["2425", "2324", "2223"]
    for season in FD_SEASONS:
        for code, league_name in FD_LEAGUES.items():
            url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
            try:
                raw = _get(url).decode("utf-8-sig", errors="replace")
            except Exception:
                continue
            if "HomeTeam" not in raw[:2000]:
                continue  # blocked/HTML page - skip
            got = 0
            try:
                for r in csv.DictReader(io.StringIO(raw)):
                    hg, ag = (r.get("FTHG") or "").strip(), (r.get("FTAG") or "").strip()
                    home, away = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
                    if not (hg and ag and home and away):
                        continue
                    d_raw = (r.get("Date") or "").strip()
                    try:
                        d, mth, y = d_raw.split("/")
                        y = ("20" + y) if len(y) == 2 else y
                        iso = f"{y}-{int(mth):02d}-{int(d):02d}"
                    except Exception:
                        iso = d_raw
                    rows.append([iso, league_name, home, away, int(float(hg)), int(float(ag))])
                    got += 1
            except Exception:
                continue
            print(f"  football-data {season} {league_name:<18} {got:>4}")

# ---------- dedupe + write ----------
seen = set()
final: list[list] = []
for r in rows:
    k = (r[0], r[2], r[3])
    if k in seen:
        continue
    seen.add(k)
    final.append(r)

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["date", "league", "home_team", "away_team",
                "home_goals", "away_goals"])
    w.writerows(final)

print(f"\nWROTE {len(final)} unique matches -> {OUT}")
print("Next:  python -m models.strength_engine")