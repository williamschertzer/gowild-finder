"""Is the pass paying for itself? Savings per trip versus what the pass cost."""
import math
from datetime import date


def summarize(trips, pass_price: float, pass_end: date, today: date | None = None) -> dict:
    today = today or date.today()
    saved = sum((t["cash_fare"] or 0) - (t["paid"] or 0) for t in trips)
    paid = sum(t["paid"] or 0 for t in trips)
    n = len(trips)
    avg = saved / n if n else None
    remaining = max(pass_price - saved, 0)
    return {
        "trips": n,
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
