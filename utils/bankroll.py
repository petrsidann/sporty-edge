"""
Bankroll management and staking policies.

Flat staking
------------
Constant stake per bet; the recommended default while calibration is young.

Fractional Kelly
----------------
    f* = (p * o - 1) / (o - 1)
scaled by kelly_fraction (default quarter Kelly) and hard-capped per bet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import STAKING_SETTINGS


@dataclass(frozen=True)
class SettlementRecord:
    """A single settled bet in the bankroll's in-memory history."""

    stake_units: float
    decimal_odds: float
    outcome: str
    profit_currency: float
    bankroll_after: float
    settled_at: str


class Bankroll:
    """Bankroll state, stake sizing, and simple risk controls."""

    def __init__(
        self,
        initial_bankroll: float | None = None,
        unit_size: float | None = None,
        kelly_fraction: float | None = None,
        max_stake_pct_bankroll: float | None = None,
        max_daily_exposure_pct: float | None = None,
        max_units_per_bet: float | None = None,
        max_drawdown_pct: float | None = None,
    ) -> None:
        s = STAKING_SETTINGS
        self.initial_bankroll = float(
            initial_bankroll if initial_bankroll is not None else s.initial_bankroll
        )
        self.unit_size = float(unit_size if unit_size is not None else s.unit_size)
        self.kelly_fraction = float(
            kelly_fraction if kelly_fraction is not None else s.kelly_fraction
        )
        self.max_stake_pct_bankroll = float(
            max_stake_pct_bankroll
            if max_stake_pct_bankroll is not None
            else s.max_stake_pct_bankroll
        )
        self.max_daily_exposure_pct = float(
            max_daily_exposure_pct
            if max_daily_exposure_pct is not None
            else s.max_daily_exposure_pct
        )
        self.max_units_per_bet = float(
            max_units_per_bet if max_units_per_bet is not None else s.max_units_per_bet
        )
        self.max_drawdown_pct = float(
            max_drawdown_pct if max_drawdown_pct is not None else s.max_drawdown_pct
        )

        if self.initial_bankroll <= 0 or self.unit_size <= 0:
            raise ValueError("Bankroll and unit size must be positive.")
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1].")

        self.current_bankroll: float = self.initial_bankroll
        self._open_stake_units: float = 0.0
        self.total_staked_currency: float = 0.0
        self.history: list[SettlementRecord] = []
        self.halted: bool = False

    def units_to_currency(self, units: float) -> float:
        return units * self.unit_size

    def currency_to_units(self, amount: float) -> float:
        return amount / self.unit_size

    def flat_stake_units(self, units: float = 1.0) -> float:
        return float(min(units, self.max_units_per_bet))

    def kelly_stake_units(self, model_probability: float, decimal_odds: float) -> float:
        """Fractional-Kelly stake in units, capped; 0.0 when there is no edge."""
        b = decimal_odds - 1.0
        if b <= 0.0:
            return 0.0
        full_kelly = (model_probability * decimal_odds - 1.0) / b
        if full_kelly <= 0.0:
            return 0.0

        fraction = full_kelly * self.kelly_fraction
        stake_currency = min(
            fraction * self.current_bankroll,
            self.max_stake_pct_bankroll * self.current_bankroll,
        )
        units = min(self.currency_to_units(stake_currency), self.max_units_per_bet)
        return round(units, 2)

    def can_place(self, stake_units: float) -> bool:
        """Risk gate: not halted, affordable, and within the exposure cap."""
        if self.halted or stake_units <= 0.0:
            return False
        if self.units_to_currency(stake_units) > self.current_bankroll:
            return False
        open_exposure = self.units_to_currency(self._open_stake_units + stake_units)
        exposure_limit = self.max_daily_exposure_pct * self.current_bankroll
        return open_exposure <= exposure_limit + 1e-9

    def register_bet(self, stake_units: float) -> None:
        if not self.can_place(stake_units):
            raise RuntimeError(
                f"Risk control rejected stake of {stake_units:.2f} units "
                f"(open exposure {self._open_stake_units:.2f} u)."
            )
        self._open_stake_units += stake_units

    def settle(self, stake_units: float, decimal_odds: float, outcome: str) -> float:
        """Settle a bet; returns profit in currency. Outcome: WIN|LOSS|VOID."""
        if outcome not in {"WIN", "LOSS", "VOID"}:
            raise ValueError('outcome must be "WIN", "LOSS" or "VOID".')

        self._open_stake_units = max(0.0, self._open_stake_units - stake_units)
        stake = self.units_to_currency(stake_units)

        if outcome == "WIN":
            profit = stake * (decimal_odds - 1.0)
        elif outcome == "LOSS":
            profit = -stake
        else:
            profit = 0.0

        self.current_bankroll += profit
        if outcome in {"WIN", "LOSS"}:
            self.total_staked_currency += stake

        self.history.append(
            SettlementRecord(
                stake_units=stake_units,
                decimal_odds=decimal_odds,
                outcome=outcome,
                profit_currency=profit,
                bankroll_after=self.current_bankroll,
                settled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )

        if self.current_bankroll <= self.initial_bankroll * (1.0 - self.max_drawdown_pct):
            self.halted = True

        return profit

    def summary(self) -> dict[str, object]:
        wins = sum(1 for r in self.history if r.outcome == "WIN")
        profit = self.current_bankroll - self.initial_bankroll
        roi = (
            profit / self.total_staked_currency
            if self.total_staked_currency > 0
            else 0.0
        )
        return {
            "initial_bankroll": round(self.initial_bankroll, 2),
            "current_bankroll": round(self.current_bankroll, 2),
            "open_exposure_units": round(self._open_stake_units, 2),
            "bets_settled": len(self.history),
            "wins": wins,
            "win_rate": round(wins / len(self.history), 4) if self.history else 0.0,
            "total_staked_currency": round(self.total_staked_currency, 2),
            "realised_profit_currency": round(profit, 2),
            "roi": round(roi, 4),
            "halted": self.halted,
        }
