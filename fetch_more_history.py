"""fetch_more_history.py - multi-sport history from free GitHub sources."""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
HDR = {"User-Agent": "Mozilla/5.0 sporty-edge"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


# ---------------- tennis (ATP + WTA, game-level, 2022-2025) ----------------
def tennis() -> None:
    out = DATA / "history_tennis.csv"
    total = 0
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "winner_name", "loser_name"])
        for repo, prefix in (("JeffSackmann/tennis_atp", "atp_matches"),
                             ("JeffSackmann/tennis_wta", "wta_matches")):
            for year in (2022, 2023, 2024, 2025):
                url = (f"https://raw.githubusercontent.com/{repo}/master/"
                       f"{prefix}_{year}.csv")
                try:
                    raw = _get(url).decode("utf-8-sig", errors="replace")
                except Exception:
                    continue
                for r in csv.DictReader(io.StringIO(raw)):
                    wn, ln, d = (r.get("winner_name") or "").strip(), \
                                (r.get("loser_name") or "").strip(), \
                                (r.get("tourney_date") or "").strip()
                    if not (wn and ln and d):
                        continue
                    try:
                        iso = datetime.strptime(d, "%Y%m%d").date().isoformat()
                    except ValueError:
                        continue
                    w.writerow([iso, wn, ln])
                    total += 1
    print(f"  tennis: {total} matches -> {out.name}")


# ---------------- NBA (game-level via 538 elo dataset) ----------------
NBA_CODES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BRK": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHO": "Charlotte Hornets",
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NOH": "New Orleans Pelicans",
    "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers",
    "PHO": "Phoenix Suns", "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz",
    "WAS": "Washington Wizards", "WSB": "Washington Wizards",
}


def nba() -> None:
    out = DATA / "history_nba.csv"
    url = ("https://raw.githubusercontent.com/fivethirtyeight/data/master/"
           "nba-elo/nbaallelo.csv")
    total = 0
    try:
        raw = _get(url).decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  NBA: fetch failed ({type(exc).__name__})")
        return
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "winner_name", "loser_name"])
        for r in csv.DictReader(io.StringIO(raw)):
            if r.get("game_result") != "W":
                continue
            code, opp = r.get("team_id"), r.get("opp_id")
            home = NBA_CODES.get(code)
            away = NBA_CODES.get(opp)
            if not home or not away:
                continue
            d = (r.get("date_game") or "").strip()
            try:
                iso = datetime.strptime(d, "%m/%d/%Y").date().isoformat()
            except ValueError:
                try:
                    iso = datetime.strptime(d, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    continue
            w.writerow([iso, home, away])
            total += 1
    print(f"  NBA: {total} games -> {out.name}")


# ---------------- NFL (game-level via 538 elo dataset) ----------------
def nfl() -> None:
    out = DATA / "history_nfl.csv"
    url = ("https://raw.githubusercontent.com/fivethirtyeight/data/master/"
           "nfl-elo/nfl_games.csv")
    total = 0
    try:
        raw = _get(url).decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  NFL: fetch failed ({type(exc).__name__})")
        return
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "winner_name", "loser_name"])
        for r in csv.DictReader(io.StringIO(raw)):
            try:
                res = int(float(r.get("result1") or -1))
            except (TypeError, ValueError):
                continue
            t1, t2 = (r.get("team1") or "").strip(), (r.get("team2") or "").strip()
            if not t1 or not t2:
                continue
            winner, loser = (t1, t2) if res == 1 else (t2, t1)
            w.writerow([(r.get("date") or "").strip(), winner, loser])
            total += 1
    print(f"  NFL: {total} games -> {out.name}")


# ---------------- MLB (season W/L via Lahman/baseballdatabank) ----------------
def mlb() -> None:
    out = DATA / "history_mlb.csv"
    url = ("https://raw.githubusercontent.com/chadwickbureau/baseballdatabank/"
           "master/core/Teams.csv")
    total = 0
    try:
        raw = _get(url).decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  MLB: fetch failed ({type(exc).__name__})")
        return
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["year", "team", "wins", "losses"])
        for r in csv.DictReader(io.StringIO(raw)):
            try:
                year = int(float(r.get("yearID") or 0))
            except (TypeError, ValueError):
                continue
            if year < 2018:
                continue
            name = (r.get("name") or "").strip()
            try:
                wl, ll = int(float(r.get("W") or 0)), int(float(r.get("L") or 0))
            except (TypeError, ValueError):
                continue
            if not name or not (wl + ll):
                continue
            w.writerow([year, name, wl, ll])
            total += 1
    print(f"  MLB: {total} team-seasons -> {out.name}")


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    tennis()
    nba()
    nfl()
    mlb()
    print("\nNext:  git add data/ ; git commit -m 'multi-sport history' ; git push")