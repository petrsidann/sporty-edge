"""
Platform-ready pick sheets and the booking-code workflow.

How a sheet is built:
    reference book : the data-source book with the best coverage/odds
                     (from the feed — e.g. Pinnacle, 1xBet). Read-only:
                     no account, and most are not usable from Kenya.
    load platform  : the first app you hold an account with
                     (PLACEABLE_BOOKS: SportyBet / Betika / BetPawa / Betfalme).

Each leg shows the reference odds and the MINIMUM price at which to place it
on your app (reference * (1 - PRICE_TOLERANCE)).  If your app's price is at
or above that minimum, place the leg; if it is far below, skip the leg —
the value is gone.

The booking code itself is minted by the platform's app when you assemble
the picks there (SportyBet 'Book', Betika 'Book Bet').  The system then
registers the code the app gives you into the ledger for tracking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config.settings import PLACEABLE_BOOKS, PRICE_TOLERANCE, PRIORITY_BOOKS
from slips.generator import Slip


@dataclass(frozen=True)
class SheetLine:
    """One numbered line on a platform pick sheet."""

    index: int
    match_label: str
    pick: str
    odds: float            # reference odds from the best data source
    source: str            # which data source supplied the reference price
    place_min_odds: float  # place the leg on your app only at/above this


@dataclass(frozen=True)
class PlatformSheet:
    """A platform-targeted pick sheet for one slip."""

    slip: Slip
    platform: str          # the app to load on (a PLACEABLE_BOOK)
    reference_book: str    # the data source supplying reference odds
    lines: tuple[SheetLine, ...]
    combined_odds: float   # product of reference odds
    coverage: int          # how many legs have reference prices

    @property
    def ev_per_unit(self) -> float:
        return self.slip.combined_prob * self.combined_odds - 1.0

    def render(self, bet_id: str = "") -> str:
        width = 70
        bar = "=" * width
        sub = "-" * width
        bet_ref = bet_id or f"BET-{self.slip.slip_id}"

        out = [bar]
        out.append(
            f" PICK SHEET {bet_ref}  |  {self.slip.slip_type} ({self.slip.n_legs} picks)"
        )
        out.append(f" LOAD ON >>> {self.platform.upper()}")
        out.append(
            f" Reference odds from: {self.reference_book}  "
            f"({self.coverage}/{self.slip.n_legs} legs priced)"
        )
        if self.slip.slip_type == "SINGLE":
            out.append(" PLACE AS A SINGLE — one pick, stake exactly as shown.")
        out.append(sub)
        for ln in self.lines:
            out.append(
                f" {ln.index}. {ln.match_label:<32} {ln.pick:<16} "
                f"ref @ {ln.odds:<5.2f}  place if ≥ {ln.place_min_odds:.2f}"
            )
        out.append(sub)
        out.append(f" Combined ref odds : {self.combined_odds:.2f}")
        out.append(
            f" True win prob     : {self.slip.combined_prob * 100:.1f}%"
            f"  ->  expected to land ~{self.slip.combined_prob * 10:.0f} of 10"
        )
        out.append(f" EV per unit       : {self.ev_per_unit * 100:+.1f}%")
        out.append(f" Stake             : {self.slip.stake_units:.2f} units")
        out.append(sub)
        out.append(" HOW TO LOAD:")
        for i, step in enumerate(load_steps(self.platform, bet_ref), start=1):
            out.append(f"   {i}. {step}")
        out.append(bar)
        return "\n".join(out)


def choose_platform(
    slip: Slip, priority_books: Sequence[str] | None = None
) -> PlatformSheet | None:
    """Build a pick sheet: reference odds from the best data book, load target
    from the app list you actually hold accounts with."""
    books: list[str] = list(priority_books) if priority_books else list(PRIORITY_BOOKS)
    for leg in slip.legs:
        for book in leg.quotes_by_book:
            if book not in books:
                books.append(book)

    # ---- reference book: most legs priced, then best combined price ---- #
    best_key: tuple[int, float, int] | None = None
    reference_book: str | None = None

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
            reference_book = book

    if reference_book is None or best_key is None:
        return None

    # ---- load target: first app you hold an account with ---- #
    platform = PLACEABLE_BOOKS[0] if PLACEABLE_BOOKS else reference_book

    sheet_lines: list[SheetLine] = []
    combined = 1.0
    for i, leg in enumerate(slip.legs, start=1):
        pick = f"{leg.market} -> {leg.selection}"
        ref_odds = leg.quotes_by_book.get(reference_book, leg.decimal_odds)
        combined *= ref_odds
        sheet_lines.append(
            SheetLine(
                index=i,
                match_label=leg.match_label,
                pick=pick,
                odds=ref_odds,
                source=reference_book,
                place_min_odds=round(ref_odds * (1.0 - PRICE_TOLERANCE), 2),
            )
        )

    return PlatformSheet(
        slip=slip,
        platform=platform,
        reference_book=reference_book,
        lines=tuple(sheet_lines),
        combined_odds=combined,
        coverage=best_key[0],
    )


def load_steps(platform: str, bet_id: str) -> list[str]:
    """Numbered instructions for loading one slip on your app."""
    return [
        f"Open {platform} and add the picks above, in the same order.",
        "For each leg: if your app's odds are ≥ the 'place if ≥' number, keep "
        "it. If clearly below, skip that leg — the value has moved.",
        "Set your stake (the sheet shows units; 1 unit = your configured size).",
        "Tap Book / Book Bet and copy the booking code the app gives you.",
        "Register the code so the ledger tracks this slip:",
        (
            f"   python3 -c \"from utils.logger import BetLogger; "
            f"BetLogger().attach_code('{bet_id}','{platform}','YOUR-CODE')\""
        ),
        "After the matches finish, settle the slip (WIN / LOSS / VOID).",
    ]