"""SQLite storage for saved searches, fare checks and logged trips."""
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("GOWILD_DB", os.path.join(os.path.dirname(__file__), "..", "gowild.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    origins TEXT NOT NULL,          -- comma-separated IATA codes
    destinations TEXT NOT NULL,
    start_date TEXT NOT NULL,       -- ISO dates
    end_date TEXT NOT NULL,
    weekdays TEXT NOT NULL DEFAULT '0123456', -- Mon=0 .. Sun=6
    include_blackouts INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    auto INTEGER NOT NULL DEFAULT 0,        -- searched automatically on a schedule
    auto_hours INTEGER NOT NULL DEFAULT 3   -- how often, when auto is on
);
CREATE TABLE IF NOT EXISTS checks (
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    date TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,           -- ok | no_flights | unparsed | blocked | error
    flights TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (origin, dest, date)
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    depart_time TEXT,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    flight_no TEXT,
    cash_fare REAL NOT NULL DEFAULT 0,  -- what the same flight cost without the pass
    paid REAL NOT NULL DEFAULT 0,       -- what you paid with GoWild (taxes, fees, early charge)
    confirmation TEXT,
    notes TEXT,
    arrive TEXT                         -- local arrival 'YYYY-MM-DDTHH:MM', when known
);
CREATE TABLE IF NOT EXISTS seat_snapshots (
    trip_id INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    seats TEXT NOT NULL             -- JSON list from seats.parse_seats
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30)  # check + tomorrow runs can write at once
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(searches)")}
    if "include_blackouts" not in cols:  # added after the first release
        conn.execute("ALTER TABLE searches ADD COLUMN include_blackouts INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    if "position" not in cols:  # user-chosen order; 0 everywhere falls back to newest first
        conn.execute("ALTER TABLE searches ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "auto" not in cols:  # per-search automatic checks
        conn.execute("ALTER TABLE searches ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE searches ADD COLUMN auto_hours INTEGER NOT NULL DEFAULT 3")
        conn.commit()
    if "arrive" not in {r["name"] for r in conn.execute("PRAGMA table_info(trips)")}:
        conn.execute("ALTER TABLE trips ADD COLUMN arrive TEXT")
        conn.commit()
    return conn


def split_codes(s: str) -> list[str]:
    return [c.strip().upper() for c in s.replace(" ", ",").split(",") if c.strip()]


def save_check(conn, origin, dest, d, status, flights) -> bool:
    """Store a lookup. Returns True if this date newly has GoWild flights."""
    prev = conn.execute("SELECT status, flights FROM checks WHERE origin=? AND dest=? AND date=?", (origin, dest, d)).fetchone()
    had = bool(prev and json.loads(prev["flights"]))
    if status in ("blocked", "error") and prev:
        return False  # a failed lookup shouldn't wipe what an earlier one found
    conn.execute(
        "INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?)",
        (origin, dest, d, datetime.now().isoformat(timespec="seconds"), status, json.dumps(flights)),
    )
    conn.commit()
    return bool(flights) and not had


def get_checks(conn, origins, dests, start, end):
    q = (
        f"SELECT * FROM checks WHERE origin IN ({','.join('?' * len(origins))}) "
        f"AND dest IN ({','.join('?' * len(dests))}) AND date BETWEEN ? AND ? ORDER BY date"
    )
    rows = conn.execute(q, [*origins, *dests, start, end]).fetchall()
    return [dict(r, flights=json.loads(r["flights"])) for r in rows]


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
    conn.commit()


def save_seats(conn, trip_id, seats):
    conn.execute(
        "INSERT INTO seat_snapshots VALUES (?,?,?)",
        (trip_id, datetime.now().isoformat(timespec="seconds"), json.dumps(seats)),
    )
    conn.commit()


def latest_seats(conn, trip_id):
    row = conn.execute(
        "SELECT * FROM seat_snapshots WHERE trip_id=? ORDER BY captured_at DESC LIMIT 1", (trip_id,)
    ).fetchone()
    return (row["captured_at"], json.loads(row["seats"])) if row else (None, [])


def import_trips(conn, trips) -> int:
    """Add imported flights not already logged, then fill cash fares from what checks saw."""
    added = 0
    for t in trips:
        dup = conn.execute(
            "SELECT 1 FROM trips WHERE date=? AND origin=? AND dest=? AND (flight_no=? OR flight_no IS NULL OR flight_no='')",
            (t["date"], t["origin"], t["dest"], t["flight_no"]),
        ).fetchone()
        if dup:
            if t.get("arrive"):  # fill in what an earlier import didn't know
                conn.execute("UPDATE trips SET arrive=? WHERE date=? AND origin=? AND dest=? AND flight_no=? AND arrive IS NULL",
                             (t["arrive"], t["date"], t["origin"], t["dest"], t["flight_no"]))
            continue
        conn.execute(
            "INSERT INTO trips (date, depart_time, origin, dest, flight_no, cash_fare, paid, confirmation, notes, arrive) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t["date"], t["depart_time"], t["origin"], t["dest"], t["flight_no"], 0.0, t.get("paid") or 0.0, t.get("confirmation"),
             "imported; add cash fare", t.get("arrive")),
        )
        added += 1
    conn.commit()
    fill_cash_fares(conn)
    return added


def _flight_nos(trip) -> list[str]:
    return [n.strip() for n in str(trip["flight_no"] or "").replace(" ", ",").split(",") if n.strip()]


def journeys(trips) -> list[list[dict]]:
    """Group logged flights into journeys: a connection is the next flight of the same
    booking, leaving from where the last one landed, the same or the next day.
    Frontier prices a connecting itinerary as one fare, so that's the unit to compare."""
    out = []
    for t in sorted(trips, key=lambda t: (t["date"], t["depart_time"] or "")):
        prev = out[-1][-1] if out else None
        if prev and t["confirmation"] and t["confirmation"] == prev["confirmation"] and t["origin"] == prev["dest"] \
                and 0 <= (datetime.fromisoformat(t["date"]) - datetime.fromisoformat(prev["date"])).days <= 1:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def cash_fare_for(conn, journey) -> dict | None:
    """The fare check that saw this exact itinerary (same route, date and flight numbers)."""
    nos = {n for t in journey for n in _flight_nos(t)}
    if not nos:
        return None
    row = conn.execute("SELECT flights FROM checks WHERE origin=? AND dest=? AND date=?",
                       (journey[0]["origin"], journey[-1]["dest"], journey[0]["date"])).fetchone()
    for f in json.loads(row["flights"]) if row else []:
        if set(map(str, f.get("flight_numbers", []))) == nos and f.get("standard_fare"):
            return f
    return None


def fill_cash_fares(conn) -> int:
    """Give trips with no cash fare the price Frontier asked for the same itinerary without
    GoWild, split across its flights. Where the trip's GoWild cost isn't known, use the
    GoWild fare the check saw (fare plus taxes, the like-for-like comparison)."""
    trips = [dict(r) for r in conn.execute("SELECT * FROM trips")]
    filled = 0
    for j in journeys(trips):
        if all(t["cash_fare"] for t in j):
            continue
        f = cash_fare_for(conn, j)
        if not f:
            continue
        n = len(j)
        for t in j:
            if t["cash_fare"]:
                continue
            paid = t["paid"] or round((f.get("gowild_price") or 0) / n, 2)
            conn.execute("UPDATE trips SET cash_fare=?, paid=?, notes=REPLACE(COALESCE(notes,''), '; add cash fare', '') WHERE id=?",
                         (round(f["standard_fare"] / n, 2), paid, t["id"]))
            filled += 1
    conn.commit()
    return filled


def trips_missing_fares(conn, after: str) -> list[tuple[str, str, str]]:
    """(origin, dest, date) lookups that would price upcoming journeys with no cash fare."""
    trips = [dict(r) for r in conn.execute("SELECT * FROM trips")]
    return sorted({(j[0]["origin"], j[-1]["dest"], j[0]["date"]) for j in journeys(trips)
                   if j[0]["date"] > after and not all(t["cash_fare"] for t in j)})


def tomorrow_searches(conn, searches):
    """The saved searches picked on the Fly tomorrow page (all of them until you pick)."""
    raw = get_setting(conn, "tomorrow_ids")
    if raw is None:
        return searches
    ids = {int(i) for i in raw.split(",") if i}
    return [s for s in searches if s["id"] in ids]


SEARCH_ORDER = "ORDER BY position, id DESC"


def ordered_searches(conn):
    return [dict(r) for r in conn.execute(f"SELECT * FROM searches {SEARCH_ORDER}")]


def top_position(conn) -> int:
    """Position for a new search so it lands at the top of the list."""
    return (conn.execute("SELECT MIN(position) FROM searches").fetchone()[0] or 0) - 1


def move_search(conn, sid: int, step: int):
    """Move a search up (step=-1) or down (step=1) one place."""
    ids = [s["id"] for s in ordered_searches(conn)]
    if sid not in ids:
        return
    i = ids.index(sid)
    j = i + step
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    conn.executemany("UPDATE searches SET position=? WHERE id=?", list(enumerate(ids)))
    conn.commit()
