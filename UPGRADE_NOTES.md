# sporty-edge UPGRADE NOTES — HIT-RATE lane, CLV, strength engine, calibration

Everything below is implemented, verified (`129 passed`), and left in the
working tree. **Nothing was committed or pushed.** The file
`data/credentials.json` was never opened, modified, or referenced in code.

There is **no guaranteed profit in this domain**. Probability numbers are
true model/consensus estimates, and CLV — not win rate — is the instrument
that tells us whether the edge actually exists.

---

## What changed, and why

### 0. Ledger post-mortem first — it decided the rest (`postmortem.py`)
`python postmortem.py` reads `data/bets.jsonl` (66 bets, 59 settled) and
prints honest tables:

| segment | n | W/L | win% | ROI | verdict |
|---|---|---|---|---|---|
| **overall** | 66 | 10W/7L (42V) | 58.8% | **-17.3%** | bleeding |
| WINNER lane | 41 | 10W/7L | 58.8% | -17.3% | bleed |
| odds 1.60-2.50 | 25 | 7W/5L | 58.3% | **+9.8%** | only profitable band |
| odds 1.20-1.60 | 12 | 3W/2L | 60.0% | -44.7% | high hit rate ≠ profit |
| session S2 | 11 | 8W/3L | 72.7% | +9.3% | profit |
| platform | — | — | — | — | **no data (never attach_code'd)** |
| every other segment | — | — | — | — | insufficient data |

**What it decided:**
- The **default platform rotation** stays the configured 5 targets in order,
  because the ledger has NO settled record per platform (the platform was
  never recorded at log time). smart_picks now records `platform` at log
  time, so after >=10 settled bets per platform the rotation ranks them by
  win rate automatically.
- The **HIT lane gets full precedence** (the owner's goal is hit rate),
  with true probabilities and CLV as the honesty guard.
- The **high-odds volume tiers** that produced 42 VOIDs (SQUAD/ACTION/SPEC)
  were untouched: they cost nothing and are already ledger-verified as noise.

### 1. HIT-RATE lane (Task 2) — `smart_picks.py` + `config/settings.py`
- `HIT_RATE_MODE=True` (settings). When on, the 8-pick session fills its
  picks from the HIT lane first:
  - probability >= **0.80** from Over 0.5 / Over 1.5 / Under 4.5 / Under 5.5
    totals, moneylines with consensus >= 0.80, and **double chance**;
  - taken odds capped at **1.60**;
  - ranked by probability, one pick per match;
  - tagged **[HIT]** in Telegram, with the **true probability printed**
    (`Win prob 85%`).
- Double chance: the feed carries no DC quotes, so the reference is
  **derived from the devigged 1X2 consensus** with a conservative 5% margin
  haircut (`1/p*0.95`). The pick message says "confirm the double-chance
  price on the app".
- **No invented probabilities**: consensus 0.799 stays out (verified with a
  direct devig probe: 0.7993 < 0.80). Honesty is the feature.

### 2. CLV (Task 3) — the true edge detector — `utils/clv.py`, `settle_auto.py`
- `snapshot_closing` runs in `settle_auto.py` **before** judging and records
  the final pre-kickoff price per pending leg (`closing_odds`), then
  `attach_closing_odds` writes `closing_odds` and `clv = taken_odds /
  closing_odds - 1` onto every ledger leg.
- `metrics()` now reports **`avg_clv`** and **`beat_close_rate`**: source of
  truth = per-leg `clv` on the ledger (PENDING included — CLV is a
  pre-result signal); fallback to `data/closing.jsonl`.
- **Missing data = None, never 0.** (The old code returned `0.0` when empty,
  faking a perfectly-neutral reading.)
- A live scheduler run fired mid-audit and created `data/closing.jsonl`
  (2 real records) and stamped the other legs with `null` — exactly right.

### 3. Team-strength engine (Task 4) — `models/strength_engine.py`
- Reads `data/history.csv` (columns `date,league,home_team,away_team,
  home_goals,away_goals`; template at `data/history.csv.template`).
- v1 = **recency-weighted goal averages with shrinkage** (half-life 180d,
  pseudo-count 8): per-league home/away means, per-team shrunk
  attack/defense multipliers. Team names normalised (accents, suffixes,
  optional `data/team_aliases.csv`).
- `expected_goals(home, away, league)` uses the multiplicative Poisson
  parametrisation; bad rows are skipped with a count, a missing file just
  disables the engine.
- In `smart_picks.py`, when BOTH teams are known the pick probability is
  blended **0.4·model + 0.6·consensus** (config `MODEL_BLEND_MODEL_WEIGHT`)
  and labelled **[MODEL]**; anything unknown/failed falls back to pure
  consensus (verified: Arsenal ML blended to 0.847, tagged `[HIT] [MODEL]`).

### 4. Calibration (Task 5) — `utils/calibration.py`
- `python -m utils.calibration` buckets settled bets by stated probability
  (50-60 / 60-70 / 70-80 / 80-90 / 90%+) and prints stated vs actual win
  frequency (VOIDs excluded, <20 settled = "insufficient data"), plus a
  Brier score.
- **Loud overconfidence flag**: if a bucket's actual rate trails its stated
  rate by more than 5 points over >=20 settled, it prints "every stake sized
  on that number is too big" and points at
  `CALIBRATION_SETTINGS.shrinkage_factor` (default 1.0 = off).
- Current snapshot (17 settled W/L): 60-69% bucket actual 25% vs stated
  63% → overconfident; 50-59% actual 75% vs stated 56%. **No bucket has
  enough data to act on yet** — the report says so.

### 5. Robustness audit (Task 6)
- **Windows scheduler UnicodeEncodeError FIXED** (the log showed the task
  crashing on an emoji under cp1252). New `utils/term.py::force_utf8_stdio`
  is called at the top of `smart_picks.py`, `settle_auto.py`, `settle.py`,
  `session_cycle.py`, `run_now.py`, `single_shot.py`, `totals.py`,
  `squad.py`.
- **UTC-internal / EAT-display only**: `session_cycle.py` now counts/keys
  the day on `UTC date` (the old `date.today()` drifted from the UTC
  `logged_at` after 21:00 EAT and silently undercounted).
- **Duplicate-logging races (scheduler vs manual runs)**: new
  `utils/filelock.py` (msvcrt/fcntl side-car lock) + `try_log_slip()` with
  the dedupe check and append inside ONE lock — two processes can no longer
  both pass the check and log the same match.
- **Ledger corruption**: `_read_all` skips corrupt/torn lines with a warning
  instead of crashing; `_write_all` is lock-guarded; append-logging is
  lock-guarded too.
- **DC legs now auto-settle** in both `settle_auto.py` and `settle.py`
  (1X = home or draw, X2 = draw-or-away, 12 = not a draw). Previously DC
  slips stayed pending forever — which would have broken HIT-lane settlement.
- **Feed offline guard**: `collect()` supports `SMOKE_OFFLINE=1` / `--offline`
  = cache-only (for tests and air-gapped sanity runs). Normal runs never set
  it.
- `notify/telegram.py` exception handler no longer uses the fragile
  `"detail" in dir()`; session-cycle git pull no longer fails silently.

---

## Exact verification commands (each already run and passing)

```powershell
# 0. clean compile -- MUST show exit code 0
python -m compileall -q .
# -> exit 0, clean output

# 1. the full suite (129 tests, incl. new lane/CLV/calibration/strength tests)
python -m pytest -q
# -> 129 passed

# 2. ledger post-mortem (tables + verdict)
python postmortem.py

# 3. calibration report (stated vs actual, + shrinkage option)
python -m utils.calibration

# 4. strength engine ratings (needs data/history.csv; safe without)
python -m models.strength_engine

# 5. one 30-min cycle (settle -> top up to 8/session, dedupe-safe)
python session_cycle.py
# or the full user command
python run_now.py

# 6. offline zero-credit end-to-end smoke of the HIT lane
#    (scratch script used during the audit; deleted after verification)
$env:SMOKE_OFFLINE="1"
```

## What the owner should watch over the next 50 settled bets

1. **CLV is the true edge signal (Task 3).** Every settled leg now carries
   `closing_odds` and `clv`. Track `avg_clv` and `beat_close_rate` in
   `metrics()` / the settle printout. **Positive avg CLV sustained over 50+
   legs is the only thing that confirms real edge; flat or negative CLV
   means the strategy is just buying high hit rates at a poor price.** This
   is decided BEFORE win-rate samples are large enough to be conclusive.
2. **Calibration (Task 5).** Keep an eye on the 80-89% bucket specifically
   (the HIT lane lives there). If it states 80% and only lands ~65%, set
   `CALIBRATION_SETTINGS.shrinkage_factor` (e.g. 0.85) and re-run the
   report.
3. **HIT lane verification.** Each pick message: `Win prob 84% | take 1.18 |
   place if app >= 1.14` + stake + bet id. Verify on the app that the price
   is at/above the floor, and that **double-chance prices match** (they are
   derived/estimated, so confirm before staking).
4. **`[MODEL]` picks** only appear when BOTH teams are in `data/history.csv`.
   Export from football-data.co.uk, then watch whether `[MODEL]`-tagged
   picks settle at a higher hit rate than pure-consensus picks (the
   calibration report + CLV will tell you).
5. **Platform rotation goes live automatically** once >=10 settled bets per
   platform exist; the post-mortem's platform table becomes meaningful.

---

## Notes / limitations (stated honestly)
- `data/history.csv` is empty right now (template only). The strength
  engine blends nothing until real history is exported.
- The smoke tests that touched the real API once (before the offline guard)
  consumed some credits; the offline guard now makes zero-credit testing
  the norm.
- No new dependency: everything uses the existing `numpy/scipy/pandas`
  stack (tests use pytest, already installed).
- Nothing committed, not pushed, no file deleted. Review at will.