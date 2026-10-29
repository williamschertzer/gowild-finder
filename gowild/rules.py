"""Frontier GoWild pass rules that the app needs to know about.

Source: flyfrontier.com/deals/gowild-pass and the Fall/Winter 2026 press release
(checked 2026-10-29). Update BLACKOUTS when Frontier posts new dates.
"""
from datetime import date, datetime, timedelta

# Fall/Winter 2026 pass: valid through Feb 28, 2027.
PASS_NAME = "Fall/Winter 2026"
PASS_PRICE = 199.00
PASS_END = date(2027, 2, 28)

# Remaining blackout dates on the pass (Frontier publishes them on the pass page).
_BLACKOUT_RANGES = [
    ("2026-11-24", "2026-11-25"), ("2026-11-28", "2026-11-30"),
    ("2026-12-19", "2026-12-24"), ("2026-12-26", "2026-12-31"),
    ("2027-01-01", "2027-01-03"), ("2027-01-14", "2027-01-15"), ("2027-01-18", "2027-01-18"),
    ("2027-02-11", "2027-02-12"), ("2027-02-15", "2027-02-15"),
]


def _expand(ranges):
    out = set()
    for a, b in ranges:
        d, end = date.fromisoformat(a), date.fromisoformat(b)
        while d <= end:
            out.add(d)
            d += timedelta(days=1)
    return out


BLACKOUTS = _expand(_BLACKOUT_RANGES)

# Frontier destinations outside the US (GoWild opens 10 days out instead of 1).
INTERNATIONAL = {
    "CUN", "PVR", "SJD", "GDL", "MTY", "MEX", "CZM", "PUJ", "SDQ", "STI", "POP",
    "MBJ", "KIN", "NAS", "SAL", "GUA", "SJO", "LIR", "BZE", "AUA", "PLS", "BOG",
    "MDE", "CTG", "LIM", "YYZ", "YUL", "YVR", "YYC",
}


def is_international(origin: str, dest: str) -> bool:
    return origin.upper() in INTERNATIONAL or dest.upper() in INTERNATIONAL


def standard_window_days(origin: str, dest: str) -> int:
    """How many days before departure regular GoWild booking opens."""
    return 10 if is_international(origin, dest) else 1


def is_blackout(d: date) -> bool:
    return d in BLACKOUTS


def is_early_booking(d: date, origin: str, dest: str, today: date | None = None) -> bool:
    """A GoWild fare on this date is an Early Booking fare if the date is past the normal window."""
    today = today or date.today()
    return (d - today).days > standard_window_days(origin, dest)


def upgrade_window_opens(departure: datetime, tier: str) -> datetime | None:
    """When an elite member can request a complimentary UpFront Plus upgrade.

    Upgrades need check-in first (check-in opens 24h out). Tier windows per
    Frontier Miles elite benefits; Silver/none don't get UpFront Plus upgrades.
    """
    hours = {"gold": 4, "platinum": 12, "diamond": 24}.get((tier or "").lower())
    return departure - timedelta(hours=hours) if hours else None


def checkin_opens(departure: datetime) -> datetime:
    return departure - timedelta(hours=24)
