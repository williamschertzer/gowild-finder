"""Turn saved searches into the list of (origin, destination, date) lookups to run."""
from datetime import date, datetime, timedelta

from . import rules
from .db import split_codes


def dates_for(search, today: date | None = None):
    today = today or date.today()
    start = max(date.fromisoformat(search["start_date"]), today + timedelta(days=1))
    end = min(date.fromisoformat(search["end_date"]), rules.PASS_END)
    weekdays = {int(c) for c in search["weekdays"]}
    d = start
    while d <= end:
        # blackout dates need a Peak Day charge when Frontier offers them at all; opt-in per search
        if d.weekday() in weekdays and (search.get("include_blackouts") or not rules.is_blackout(d)):
            yield d
        d += timedelta(days=1)


def plan(conn, searches, max_age_hours: float = 12, today: date | None = None):
    """Lookups that are due, soonest dates first, skipping ones checked recently."""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    fresh = {
        (r["origin"], r["dest"], r["date"])
        for r in conn.execute("SELECT origin, dest, date FROM checks WHERE checked_at >= ?", (cutoff,))
    }
    tasks = set()
    for s in searches:
        for d in dates_for(s, today):
            for o in split_codes(s["origins"]):
                for dst in split_codes(s["destinations"]):
                    if o != dst and (o, dst, d.isoformat()) not in fresh:
                        tasks.add((o, dst, d.isoformat()))
    return sorted(tasks, key=lambda t: (t[2], t[0], t[1]))


def tomorrow_tasks(searches, today: date | None = None):
    """Every route in your saved searches, for tomorrow (the regular GoWild window)."""
    d = ((today or date.today()) + timedelta(days=1)).isoformat()
    routes = {
        (o, dst)
        for s in searches
        for o in split_codes(s["origins"])
        for dst in split_codes(s["destinations"])
        if o != dst
    }
    return [(o, dst, d) for o, dst in sorted(routes)]
