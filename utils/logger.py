"""
Bet ledger (JSON Lines) + performance metrics.

Each slip is one line: picks, odds, platform, booking code, session, and
settlement status.  Writes are atomic (temp file + rename) so the ledger
can never be left half-written by a crash.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from slips.generator import Slip

DEFAULT_LEDGER_PATH = Path("data") / "bets.jsonl"

VALID_OUTCOMES = {"WIN", "LOSS", "VOID"}

_SUMMARY_COLUMNS = [
    "bet_id", "logged_at", "slip_type", "n_legs", "primary_book",
    "combined_odds", "stake_units", "ev_per_unit", "session", "status",
    "profit_units",
]


class BetLogger:
    """Append-only JSONL ledger of recommended slips plus performance metrics."""

    def __init__(self, path: str | Path = DEFAULT_LEDGER_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------ Writing ----------------------------- #

    def log_slip(self, slip: Slip, session: str | None = None) -> str:
        """Persist a slip as PENDING and return its bet id."""
        bet_id = f"BET-{slip.slip_id}"
        record: dict[str, Any] = {
            "bet_id": bet_id,
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "slip_type": slip.slip_type,
            "n_legs": slip.n_legs,
            "legs": [asdict(leg) for leg in slip.legs],
            "combined_odds": round(slip.combined_odds, 4),
            "combined_prob": round(slip.combined_prob, 6),
            "ev_per_unit": round(slip.ev_per_unit, 6),
            "stake_units": slip.stake_units,
            "session": session,
            "platform": None,
            "booking_code": None,
            "status": "PENDING",
            "settled_at": None,
            "payout_units": 0.0,
            "profit_units": 0.0,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return bet_id

    def attach_code(self, bet_id: str, platform: str, code: str) -> dict[str, Any]:
        """Record the platform's booking code / bet reference for a logged bet."""
        if not platform or not code:
            raise ValueError("platform and code must be non-empty.")
        records = self._read_all()
        target: dict[str, Any] | None = None
        for rec in records:
            if rec.get("bet_id") == bet_id:
                target = rec
                break
        if target is None:
            raise KeyError(f"Bet id {bet_id} not found in {self.path}.")
        target["platform"] = platform
        target["booking_code"] = code
        target["code_attached_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        self._write_all(records)
        return target

    # ----------------------------- Settlement ---------------------------- #

    def settle(
        self, bet_id: str, outcome: str, payout_units: float | None = None
    ) -> dict[str, Any]:
        """Settle a pending bet in place; defaults: WIN pays stake*odds."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(VALID_OUTCOMES)}.")

        records = self._read_all()
        target: dict[str, Any] | None = None
        for rec in records:
            if rec.get("bet_id") == bet_id:
                target = rec
                break
        if target is None:
            raise KeyError(f"Bet id {bet_id} not found in {self.path}.")
        if target["status"] != "PENDING":
            raise ValueError(f"Bet {bet_id} is already settled ({target['status']}).")

        stake = float(target["stake_units"])
        if payout_units is None:
            if outcome == "WIN":
                payout_units = stake * float(target["combined_odds"])
            elif outcome == "VOID":
                payout_units = stake
            else:
                payout_units = 0.0

        target["status"] = outcome
        target["settled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target["payout_units"] = round(float(payout_units), 4)
        target["profit_units"] = round(float(payout_units) - stake, 4)

        self._write_all(records)
        return target

    # --------------------------- Read & metrics -------------------------- #

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        """Atomic rewrite: temp file then rename, so a crash can't corrupt."""
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self._read_all() if r["status"] == "PENDING"]

    def settled(self) -> list[dict[str, Any]]:
        return [r for r in self._read_all() if r["status"] != "PENDING"]

    def metrics(self) -> dict[str, float | int]:
        """Win rate, staked, profit, ROI over settled bets (units), plus the
        closing-line metrics (beat_close_rate, avg_clv) when a CLV history
        exists in data/closing.jsonl (see utils/clv.py)."""
        records = self._read_all()
        settled = [r for r in records if r["status"] != "PENDING"]
        staked = sum(float(r["stake_units"]) for r in settled)
        profit = sum(float(r["profit_units"]) for r in settled)
        wins = sum(1 for r in settled if r["status"] == "WIN")
        metrics: dict[str, float | int] = {
            "total_bets": len(records),
            "pending": len(records) - len(settled),
            "settled": len(settled),
            "wins": wins,
            "win_rate": round(wins / len(settled), 4) if settled else 0.0,
            "total_staked_units": round(staked, 2),
            "profit_units": round(profit, 2),
            "roi": round(profit / staked, 4) if staked > 0 else 0.0,
        }
        # Lazily imported so the ledger never depends on the feed at load time.
        from utils.clv import closing_metrics

        metrics.update(closing_metrics())
        return metrics

    def summary_frame(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for r in self._read_all():
            rows.append(
                {
                    "bet_id": r["bet_id"],
                    "logged_at": r["logged_at"],
                    "slip_type": r["slip_type"],
                    "n_legs": r["n_legs"],
                    "primary_book": r["legs"][0]["book"] if r["legs"] else "",
                    "session": r.get("session"),
                    "platform": r.get("platform"),
                    "booking_code": r.get("booking_code"),
                    "combined_odds": r["combined_odds"],
                    "stake_units": r["stake_units"],
                    "ev_per_unit": r["ev_per_unit"],
                    "status": r["status"],
                    "profit_units": r["profit_units"],
                }
            )
        if not rows:
            return pd.DataFrame(
                columns=_SUMMARY_COLUMNS + ["platform", "booking_code"]
            )
        return pd.DataFrame(rows)

    def breakdown(self, by: str = "slip_type") -> pd.DataFrame:
        """Aggregate settled performance grouped by slip_type / session / book."""
        df = self.summary_frame()
        settled = df[df["status"] != "PENDING"]
        if settled.empty:
            return pd.DataFrame(columns=["bets", "staked_units", "profit_units", "roi"])
        grouped = (
            settled.groupby(by)
            .agg(
                bets=("bet_id", "size"),
                staked_units=("stake_units", "sum"),
                profit_units=("profit_units", "sum"),
            )
            .reset_index()
        )
        grouped["roi"] = (
            grouped["profit_units"] / grouped["staked_units"].replace(0, np.nan)
        )
        return grouped