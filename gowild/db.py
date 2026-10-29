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
    active INTEGER NOT NULL DEFAULT 1
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
    notes TEXT
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
    """Add imported flights not already logged; fill fares from what checks saw."""
    added = 0
    for t in trips:
        dup = conn.execute(
            "SELECT 1 FROM trips WHERE date=? AND origin=? AND dest=? AND (flight_no=? OR flight_no IS NULL OR flight_no='')",
            (t["date"], t["origin"], t["dest"], t["flight_no"]),
        ).fetchone()
        if dup:
            continue
        cash, paid = 0.0, t.get("paid")
        row = conn.execute("SELECT flights FROM checks WHERE origin=? AND dest=? AND date=?", (t["origin"], t["dest"], t["date"])).fetchone()
        for f in json.loads(row["flights"]) if row else []:
            if t["flight_no"] in f.get("flight_numbers", []):
                cash = f.get("standard_fare") or 0.0
                paid = paid if paid is not None else f.get("gowild_price")
        conn.execute(
            "INSERT INTO trips (date, depart_time, origin, dest, flight_no, cash_fare, paid, confirmation, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (t["date"], t["depart_time"], t["origin"], t["dest"], t["flight_no"], cash, paid or 0.0, t.get("confirmation"),
             "imported" + ("" if cash else "; add cash fare")),
        )
        added += 1
    conn.commit()
    return added
