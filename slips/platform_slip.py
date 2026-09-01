"""
Platform-ready pick sheets and the booking-code workflow.

A booking code (SportyBet "Book", Betika "Book Bet") is created on the
platform's servers when the slip is assembled inside their app.  This module
directs you to the best platform, gives the exact picks in order, and then
registers the code the platform gives back into the ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config.settings import PRIORITY_BOOKS
from slips.generator import Slip

PLATFORM_HINTS: dict[str, str] = {
    "SportyBet": (
        "SportyBet supports booking codes: build the slip, open the betslip, "
        "then use 'Book'/'Share' to save it as a code."
    ),
    "Betika": (
        "Betika typically offers 'Book Bet': build the slip and save the "
        "booking code the app gives you."
    ),
    "BetPawa": (
        "BetPawa: build and place the slip; use the bet ID shown in "
        "'My Bets' as your reference."
    ),
    "Betfalme": (
        "Betfalme: build and place the slip; use the bet reference shown "
        "in 'My Bets'."
    ),
}


@dataclass(frozen=True)
class SheetLine:
    """One numbered line on a platform pick sheet."""

    index: int
    match_label: str
    pick: str
    odds: float
    source: str


@dataclass(frozen=True)
class PlatformSheet:
    """A platform-targeted pick sheet for one slip."""

    slip: Slip
    platform: str
    lines: tuple[SheetLine, ...]
    combined_odds: float
    coverage: int

    @property
    def ev_per_unit(self) -> float:
        return self.slip.combined_prob * self.combined_odds - 1.0

    def render(self, bet_id: str = "") -> str:
        width = 70
        bar = "=" * width
        sub = "-" * width
        bet_ref = bet_id or f"BET-{self.slip.slip_id}"

        out = [bar]
        out.append(f" PICK SHEET {bet_ref}  |  {self.slip.slip_type} ({self.slip.n_legs} picks)")
        out.append(
            f" LOAD ON >>> {self.platform.upper()}   "
            f"({self.coverage}/{self.slip.n_legs} picks priced there)"
        )
        if self.slip.slip_type == "SINGLE":
            out.append(" PLACE AS A SINGLE — add this one pick and stake exactly as shown.")
        out.append(sub)
        for ln in self.lines:
            out.append(
                f" {ln.index}. {ln.match_label:<34} {ln.pick:<18} "
                f"@ {ln.odds:<5.2f} [{ln.source}]"
            )
        out.append(sub)
        out.append(f" Combined odds : {self.combined_odds:.2f}")
        out.append(
            f" True win prob : {self.slip.combined_prob * 100:.1f}%"
            f"  ->  expected to land ~{self.slip.combined_prob * 10:.0f} of 10"
        )
        out.append(f" EV per unit   : {self.ev_per_unit * 100:+.1f}%")
        out.append(f" Stake         : {self.slip.stake_units:.2f} units")
        out.append(sub)
        out.append(" HOW TO LOAD:")
        for i, step in enumerate(load_steps(self.platform, bet_ref), start=1):
            out.append(f"   {i}. {step}")
        out.append(bar)
        return "\n".join(out)


def choose_platform(
    slip: Slip, priority_books: Sequence[str] | None = None
) -> PlatformSheet | None:
    """Pick the best platform for a whole slip and build its pick sheet.

    Ranking: most legs priced there -> highest combined odds there ->
    earlier priority position.  Legs missing on that platform fall back to
    the leg's best price, marked ``best @Book``.
    """
    books: list[str] = list(priority_books) if priority_books else list(PRIORITY_BOOKS)
    for leg in slip.legs:
        for book in leg.quotes_by_book:
            if book not in books:
                books.append(book)

    best_key: tuple[int, float, int] | None = None
    best_book: str | None = None

    for idx, book in enumerate(books):
        covered = [leg for leg in slip.legs if book in leg.quotes_by_book]
        if not covered:
            continue
        product = 1.0
        for leg in covered:
            product *= leg.quotes_by_book[book]
        key = (len(covered), round(product, 6), -idx)
        if best_key is None or key > best_key:
            best_key = key
            best_book = book

    if best_book is None or best_key is None:
        return None

    sheet_lines: list[SheetLine] = []
    combined = 1.0
    for i, leg in enumerate(slip.legs, start=1):
        pick = f"{leg.market} -> {leg.selection}"
        if best_book in leg.quotes_by_book:
            odds = leg.quotes_by_book[best_book]
            source = best_book
        else:
            odds = leg.decimal_odds
            source = f"best @{leg.book}"
        combined *= odds
        sheet_lines.append(
            SheetLine(
                index=i, match_label=leg.match_label, pick=pick,
                odds=odds, source=source,
            )
        )

    return PlatformSheet(
        slip=slip,
        platform=best_book,
        lines=tuple(sheet_lines),
        combined_odds=combined,
        coverage=best_key[0],
    )


def load_steps(platform: str, bet_id: str) -> list[str]:
    """Numbered, platform-aware instructions for loading one slip."""
    hint = PLATFORM_HINTS.get(
        platform,
        "Build the slip on the platform; use its bet reference if booking "
        "codes are not offered.",
    )
    return [
        f"Open {platform} and add the picks above in the same order.",
        hint,
        "Check each pick's price — reference odds are best-of-market; place "
        "if your platform's price is close, skip the leg if far below.",
        "Save the booking code / bet reference the platform gives you.",
        "Register it so the ledger and Telegram track it:",
        (
            f"   python3 -c \"from utils.logger import BetLogger; "
            f"BetLogger().attach_code('{bet_id}','{platform}','YOUR-CODE')\""
        ),
        "Button names can vary slightly by app version and country.",
    ]
