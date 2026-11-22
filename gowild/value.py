"""Is the pass paying for itself? Savings per trip versus what the pass cost."""
import math
from datetime import date

from .db import journeys


def summarize(trips, pass_price: float, pass_end: date, today: date | None = None) -> dict:
    """Saving on a trip = its cash fare (the same flights without GoWild) minus what you paid
    with GoWild. A trip with no cash fare yet has no known saving, so it's left out of the
    savings and averages (rather than counted as a loss of everything you paid)."""
    today = today or date.today()
    priced = [t for t in trips if t["cash_fare"]]
    saved = sum(t["cash_fare"] - (t["paid"] or 0) for t in priced)
    paid = sum(t["paid"] or 0 for t in trips)
    # a trip is a whole journey: a connection's two flights are one trip
    groups = journeys(trips) if all("date" in t for t in trips) else [[t] for t in trips]
    n = len(groups)
    priced_trips = sum(1 for g in groups if all(t["cash_fare"] for t in g))
    avg = saved / priced_trips if priced_trips else None
    remaining = max(pass_price - saved, 0)
    return {
        "trips": n,
        "unpriced": n - priced_trips,
        "saved": round(saved, 2),
        "paid": round(paid, 2),
        "pass_price": pass_price,
        "net": round(saved - pass_price, 2),
        "remaining": round(remaining, 2),
        "avg_saving": round(avg, 2) if avg is not None else None,
        # with no history, assume a typical one-way Frontier fare saves ~$80
        "trips_to_break_even": math.ceil(remaining / (avg if avg and avg > 0 else 80)) if remaining else 0,
        "cost_per_trip": round((pass_price + paid) / n, 2) if n else None,
        "days_left": max((pass_end - today).days, 0),
        "broke_even": saved >= pass_price,
    }
