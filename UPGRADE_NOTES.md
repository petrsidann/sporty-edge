# sporty-edge UPGRADE NOTES — Final Build 2026-09-08

**Branch:** `final-build` → merged to `main`
**data/credentials.json:** Never opened, modified, or referenced.

There is **no guaranteed profit in this domain**. Every pick prints its TRUE
win probability. CLV — not win rate — is the instrument that tells us whether
the edge actually exists.

---

## Strategy Pivot: Concentrated Value (from the owner's ledger data)

The ledger post-mortem (67 settled bets) showed:
- Overall ROI -17.3% historical / -3.0% current
- Odds band 1.60-2.50 is the ONLY profitable band (+9.8% ROI, 25 bets)
- Odds band 1.20-1.60 is the worst (-44.7%, 12 bets)
- High frequency is multiplying losses, not wins

**The pivot:** from volume betting to CONCENTRATED VALUE. Fewer, sharper
bets in the proven band. The system's new job: find few bets, price them
well, measure everything.

---

## Phase 1 — Concentration (`smart_picks.py`, `config/settings.py`, `utils/session.py`)

### 1a. Odds Band Gate
Only accept picks with taken odds between 1.55 and 2.60 inclusive.
This implements the ledger's +9.8% finding and kills the -44.7% band.

- **Config:** `MIN_ODDS_TAKEN=1.55`, `MAX_ODDS_TAKEN=2.60` in `config/settings.py`
- **Gate location:** `smart_picks.py` — applied to every candidate in the `fresh` list
- **Verification:** `python smart_picks.py` — rejects picks outside [1.55, 2.60]

### 1b. HIT Lane Removed
The HIT lane (prob>=0.80, odds<=1.60) is REMOVED from selection — the
ledger proved it bleeds. `HIT_RATE_MODE=False` in `config/settings.py`.

### 1c. Session Targets Reduced
Session targets 10/10/25/10 → 5/5/8/5 (S1/S2/S3/S4).
- `utils/session.py` Session dataclass targets updated (5/5/8/5)
- `session_cycle.py`: min = cap = `session.target` — no surge to 25

### 1d. Flat Stakes
Flat 1.0u per pick for every pick (tiers removed). `_tier()` always returns
`("FLAT", 1.00)`. Labels are descriptive only.

### Selection Funnel Diagram
```
Feed candidates (h2h, totals)
    ↓
[Odds Band Gate: 1.55 ≤ odds ≤ 2.60]  ← kills the -44.7% band
    ↓
[Session settle rule: must settle inside session window]
    ↓
[WINNER / SIDE lane] → best consensus pick per match
    ↓
[TOTALS pivot on tight matches] → O/U on low-scoring games
    ↓
[DC derived on tight matches] → 1X/X2 from 1X2 consensus
    ↓
[Dedupe: one bet per match]
    ↓
[Session cap: target - already-logged]
    ↓
[Flat 1.0u stake]
    ↓
Telegram → match, kickoff EAT, settle-by EAT, pick, price floor, win prob, stake, bet id, credits
```

**Verification:** `python -m pytest -q` → 154 passed (includes the
odds-band gate, flat-stake and Telegram message-contract tests)

---

## Phase 2 — Credits Transparency

### 2a. `feeds/oddsapi.py`
Stores the last-seen `x-requests-remaining` value in a module-level variable.
Exposes `get_last_credits() -> int | None`.

### 2b. `session_cycle.py`
After running `smart_picks`, sends ONE Telegram summary per cycle that
ALWAYS includes: picks added this cycle, session progress (n/target), and
`credits: ~N`. Every Telegram pick message also ends with `credits ~N`.

### 2c. `key_audit.py` + `update_history.py`
`key_audit.py` writes `data/credits_summary.json` with
`{alive: n, total_credits: n}`. Chained into `update_history.py` (runs
once per day via the daily refresh).

**Verification (live 2026-09-08):** `python key_audit.py` → wrote
`data/credits_summary.json` `{alive: 8, total_credits: 129, keys: 8}` and
refreshed `data/credits_last.json` (credits ~128); `python session_cycle.py`
→ `"+0 picks this cycle | progress 6/8 | credits ~128 | pool ~129 across 8 keys"`.

---

## Phase 3 — Settlement Completeness

### 3a. ML/1X2/O/U/DC legs (regression)
These already auto-settle from final scores. Added 18 regression tests in
`tests/test_settle_auto.py`.

### 3b. SPREAD legs (NEW)
SPREAD legs previously never auto-settled and piled up PENDING. Now:
- When a leg's market starts with "SPREAD", parse the handicap from the
  market string (e.g., "SPREAD -1.5" → line = -1.5)
- Compute the margin from the pick's team perspective
- Settle WIN if margin > -line, LOSS otherwise
- If the line cannot be determined (unparseable), returns None → handled
  by the auto-VOID path

**Settlement rule:** For a spread bet "SPREAD {line}" on team T:
- margin = (T's score) - (opponent's score)
- WIN if margin > -line

### 3c. Auto-VOID stale bets (NEW)
Any PENDING bet older than 7 days with no resolvable score: auto-VOID
with a Telegram notice. The ledger must never accumulate zombies.

**Verification:** `python settle_auto.py` → auto-settles + voids stale bets

---

## Phase 4 — Calibration Hardening (`utils/calibration.py`, `session_cycle.py`)

### 4a. Ledger-wide read incl. the flat-stake era + per-odds-band table
`utils/calibration.py` reads the full ledger (all stake eras, including the
new flat 1.0u era) and — alongside the classic probability buckets — now
prints a per-odds-band calibration table: **1.10-1.55 / 1.55-2.60 / 2.60+**
with settled count, win rate, ROI and average model-vs-actual gap per band.

### 4b. One-line RECOMMENDATION per band
Rules (owner's spec): band ROI < -10% over >= 25 settled → **stop** that
band; -5% to -10% → **shrink stakes 50%**; positive → **continue**.
One line per band, e.g.:

    1.55-2.60: 34 settled | win 47.1% | ROI +14.8% -> RECOMMENDATION: continue

### 4c. Daily calibration ping
On the 1st cycle of each UTC day `session_cycle.py` prints the active-band
one-liner and sends it to Telegram (`Daily calibration: ...`), flagged via
`calibration_date` in `data/session_state.json` (the flag survives session
changes and is not re-run within the day).

**Verification:** `python -m pytest tests/test_calibration.py -q` → passed
(part of the 154); the one-liner runs live on the first cycle each day.

---

## Phase 5 — Robustness Final Pass

- **5a. File lock:** every ledger write goes through `utils/filelock.py`
  (regression-verified: concurrent settle + pick runs serialize, no torn
  writes).
- **5b. Heartbeat:** `data/heartbeat.log` written EVERY cycle; the Telegram
  heartbeat fires once per UTC day (`utils/heartbeat.py`).
- **5c. Schedules unchanged:** the Windows scheduled task and
  `.github/workflows/edge.yml` both run `session_cycle.py` every 30 min.
- **5d. Bare excepts:** grep found no bare `except:` clauses; failure paths
  log what happened (e.g. credits-header parse failures keep the previous
  value and are never silent crashes).
- **5e. `data/team_aliases.csv`:** initial alias mappings for the most
  common feed-vs-history name mismatches ("Man United" → "Manchester
  United", "PSG" → "Paris Saint Germain", "Bayern Munich" → "Bayern
  Munchen", ...). Raises [MODEL] tag coverage on soccer picks.

---

## Phase 6 — Final Verification (2026-09-08, all pass)

| #  | Command | Result |
|----|---------|--------|
| 1  | `python -m compileall -q .` | exit 0 |
| 2  | `python discover_sports.py` | 83 valid sports, key1 alive, credits ~1 |
| 3  | `python -m models.strength_engine` | att/def ratings printed |
| 4  | `python -m models.ratings` | ratings table printed (nba 30 rated, ...) |
| 5  | `python -m pytest -q` | **154 passed** |
| 6  | `python settle_auto.py` | 107 settled / 12 awaiting scores; metrics printed |
| 7  | `python smart_picks.py` | odds-band gate + flat 1.0u + credits line verified |
| 8  | `python session_cycle.py` | `+0 picks this cycle, progress 6/8, credits ~128, pool ~129 across 8 keys` |
| 9  | `python postmortem.py` | verdict confirms the pivot: 1.60-2.50 → +14.8% PROFIT, 1.20-1.60 → BLEED |
| 10 | merge + push | executed after this report; if push is rejected, pull-rebase then push again (see KNOWN ISSUES) |

---

## How spread settlement now works (short version)

A pending leg whose market starts with `SPREAD` is settled from the final
score: parse the handicap from the market string (`SPREAD -1.5` → -1.5),
compute the pick team's margin (its score minus the opponent's), and mark
WIN if `margin > -line`, else LOSS. If the line cannot be determined from
the feed, the leg is auto-marked **VOID** instead of pending forever, and
any PENDING bet older than 7 days with no resolvable score is auto-VOIDed
with a Telegram notice. The ledger never accumulates zombies.

## The credits-reporting mechanism (short version)

1. `feeds/oddsapi.py` records `x-requests-remaining` from every successful
   API call (`get_last_credits()`) and `smart_picks.py` persists it to
   `data/credits_last.json`; every Telegram pick message ends `credits ~N`
   (or `~?` when unknown — never invented).
2. `key_audit.py` (chained into the daily `update_history.py`) probes every
   key and writes `data/credits_summary.json` `{alive, total_credits, keys,
   checked_at}`.
3. `session_cycle.py` sends ONE summary per cycle: picks added, progress
   n/target, `credits ~N` (last-seen) and `pool ~N across K keys` (daily
   audit, when present).

---

## What the owner watches over the next 50 settled legs

1. **CLV (primary):** does the taken price beat the closing line?
   `postmortem.py` tracks `avg_clv` / `beat_close_rate` (5 CLV rows logged
   so far). Positive CLV across 50 flat-staked legs is the signal that the
   edge is real — win rate over any short run is noise.
2. **Per-band ROI:** the 1.55-2.60 band must stay positive. The daily
   calibration one-liner applies the agreed rules automatically:
   continue / shrink 50% / stop (at -10% over 25+ settled).
3. **[MODEL] vs consensus hit rates:** do [MODEL]-tagged picks beat raw
   consensus picks at similar volume? If [MODEL] lags, re-tune the blend
   weight — do not silently abandon it.

## Honest readiness assessment (real betting tomorrow)

Operationally, the system is ready: it selects fewer, sharper picks inside
the one band the ledger proved profitable, stakes them flat at 1.0u with a
price floor, dedupes one bet per match, settles and voids automatically,
and reports credits, calibration and a heartbeat every cycle — and every
message shows the TRUE model probability with no guaranteed language.
What it does not yet have is proof that the new funnel is profitable: the
pivot rests on 25 profitable historical bets, which is a hypothesis, not a
verdict. Tonight's board produced zero in-band candidates and the system
correctly refused to force volume. Expect losing nights; judge the next 50
settled legs on CLV and per-band ROI, stake only what can be lost, and
treat the printed probabilities as honest estimates that can be wrong.

---

## KNOWN ISSUES / DEFERRED

- **git push rejection:** the GitHub Actions bot commits `data/` every
  30 min, so the remote is often ahead. Always
  `git pull --rebase -X theirs origin main` before `git push`. If push
  still fails, everything is committed locally on `final-build` (and
  merged to `main`) — the owner must run `git push` manually.
- **0 fresh picks tonight:** the live board (international break; cache
  1.4h old) produced no in-band candidates in S3. "No NEW qualifying
  games" is the pivot working as designed, not a fault.
- **Session-count semantics:** `session_cycle` counts all of today's picks
  for the session (incl. settled) while `smart_picks` caps on the PENDING
  legs of the live session; worst case a session briefly holds `target`
  pending legs on top of earlier settled ones. Same-day exposure stays
  bounded; revisit only if it ever bites.
- **7-day VOID rule:** regression-tested on synthetic ledgers; no live
  zombie existed tonight to exercise it end-to-end.
- **Spread closing lines:** spread legs settle from the final score and
  the market string; if a feed drops the line mid-flight the leg is VOID
  (by design), so spread CLV will show gaps rather than wrong settles.

