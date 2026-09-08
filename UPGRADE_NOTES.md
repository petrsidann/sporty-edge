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
- `utils/session.py` Session dataclass targets updated
- `session_cycle.py` `TARGET_MIN=5`, `TARGET_MAX=8`

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

**Verification:** `python -m pytest tests/test_smart_lanes.py -q` → 15 passed

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

**Verification:** `python feeds/oddsapi.py --test` → credits captured

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
