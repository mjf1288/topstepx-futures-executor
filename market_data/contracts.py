"""Contract calendars for the roots we collect.

Expiry dates here only bound the window of history requested for each contract
month. They ignore exchange holidays, so every window is padded; the data the
broker actually returns is what ends up in the store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

MONTH_CODES = "FGHJKMNQUVXZ"

# Listed months used for trading, and how many months apart they are.
CYCLES = {
    "MNQ": "HMUZ",
    "MES": "HMUZ",
    "MYM": "HMUZ",
    "MGC": "GJMQVZ",
    "MCL": MONTH_CODES,
}

# Minimum price increment per root. Back-adjusted prices are snapped to this
# grid so float drift can't produce prices the backtester rejects.
TICK_SIZE = {"MNQ": 0.25, "MES": 0.25, "MYM": 1.0, "MGC": 0.10, "MCL": 0.01}

# Padding before a contract becomes front month, so the continuous builder
# sees both contracts' volume across the roll.
LEAD_DAYS = 60


@dataclass(frozen=True)
class Contract:
    root: str
    month_code: str
    year: int  # four digits

    @property
    def month(self) -> int:
        return MONTH_CODES.index(self.month_code) + 1

    @property
    def contract_id(self) -> str:
        return f"CON.F.US.{self.root}.{self.month_code}{self.year % 100:02d}"

    @property
    def symbol(self) -> str:
        # Databento style (e.g. MNQZ6), matching the parquet files from Tarric.
        return f"{self.root}{self.month_code}{self.year % 10}"

    @property
    def expiry(self) -> date:
        return expiry_date(self.root, self.year, self.month)

    def fetch_window(self) -> tuple[datetime, datetime]:
        """UTC window of history worth requesting for this contract month."""
        cycle = CYCLES[self.root]
        gap_months = 12 // len(cycle)
        start = self.expiry - timedelta(days=gap_months * 31 + LEAD_DAYS)
        end = self.expiry + timedelta(days=1)
        return (
            datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
        )


def _is_business_day(d: date) -> bool:
    return d.weekday() < 5


def _step_business_days(d: date, n: int) -> date:
    """Move back n business days (n > 0)."""
    while n > 0:
        d -= timedelta(days=1)
        if _is_business_day(d):
            n -= 1
    return d


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _last_business_day(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while not _is_business_day(d):
        d -= timedelta(days=1)
    return d


def expiry_date(root: str, year: int, month: int) -> date:
    if root in ("MNQ", "MES", "MYM"):
        return _third_friday(year, month)
    if root == "MGC":
        # Third-last business day of the contract month.
        return _step_business_days(_last_business_day(year, month), 2)
    if root == "MCL":
        # CL stops 3 business days before the 25th of the prior month (or the
        # business day before the 25th if it isn't one). MCL stops one business
        # day before CL.
        py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
        anchor = date(py, pm, 25)
        while not _is_business_day(anchor):
            anchor -= timedelta(days=1)
        return _step_business_days(anchor, 4)
    raise ValueError(f"no expiry rule for {root}")


def contracts_between(root: str, since: date, until: date) -> list[Contract]:
    """Every listed contract whose fetch window overlaps [since, until]."""
    if root not in CYCLES:
        raise ValueError(f"unsupported root {root}; known: {sorted(CYCLES)}")
    out = []
    for year in range(since.year - 1, until.year + 2):
        for code in CYCLES[root]:
            c = Contract(root, code, year)
            start, end = c.fetch_window()
            if end.date() >= since and start.date() <= until:
                out.append(c)
    return sorted(out, key=lambda c: c.expiry)
