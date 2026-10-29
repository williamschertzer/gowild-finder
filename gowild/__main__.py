"""Command line: python -m gowild <command>

  check      run due lookups for your saved searches (slowly, logged out)
  tomorrow   check every saved route for tomorrow (the regular GoWild window)
  plan       show what 'check' would look up, without opening a browser
  seats      read paid seat prices for a booked trip (--trip ID); you log in yourself
  import     add your past and upcoming Frontier trips to the trip log; you log in yourself
  auto       what the scheduler runs: a check, plus tomorrow's check from noon on
  serve      open the web app at http://127.0.0.1:5050 (--phone to allow your iPhone)
"""
import argparse
import signal
import sys

from datetime import date, datetime

from . import db, notify, planner, rules
from .frontier import Blocked, FrontierBrowser


def run_lookups(conn, tasks, headless, limit, profile="browser-profile", offscreen=False):
    tasks = tasks[:limit]
    print(f"{len(tasks)} lookups to run", flush=True)
    if not tasks:
        return 0
    found, finds = 0, []
    with FrontierBrowser(headless=headless, profile=profile, offscreen=offscreen) as br:
        for i, (o, d, day) in enumerate(tasks, 1):
            try:
                status, flights = br.lookup(o, d, day)
            except Blocked as e:
                print(f"Stopping: {e}. Try again later, or run without --headless.")
                db.save_check(conn, o, d, day, "blocked", [])
                if finds:
                    notify.send(conn, *notify.new_finds_message(finds))
                return 2
            except Exception as e:  # keep going on one bad page
                if "has been closed" in str(e):  # someone closed the window: treat it as Stop
                    print("The browser window was closed, so the check stopped. What it found so far is saved.", flush=True)
                    if finds:
                        notify.send(conn, *notify.new_finds_message(finds))
                    return 3
                status, flights = "error", []
                print(f"  error: {e}", flush=True)
            if db.save_check(conn, o, d, day, status, flights):
                finds.append((o, d, day, len(flights), rules.is_early_booking(date.fromisoformat(day), o, d)))
            found += len(flights)
            print(f"[{i}/{len(tasks)}] {o}-{d} {day}: {status}, {len(flights)} GoWild", flush=True)
    print(f"Done. {found} GoWild flights found.")
    if finds and notify.send(conn, *notify.new_finds_message(finds)):
        print(f"Sent an alert for {len(finds)} new date(s).")
    return 0


def run_seats(conn, trip_id):
    from . import seats

    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip:
        print("No such trip. Log the trip on the Pass value page first.")
        return 1
    found, raw = seats.capture(f"{trip['origin']}-{trip['dest']} on {trip['date']}")
    if not found:
        print("No seat map data seen." + (" Saved what was captured to debug/." if raw else ""))
        return 1
    db.save_seats(conn, trip_id, found)
    for g in seats.summarize(found):
        rng = f"${g['min']:.0f}" + (f"-${g['max']:.0f}" if g["max"] != g["min"] else "") if g["min"] is not None else "price ?"
        print(f"  {g['type']}: {g['open']} open, {rng}")
    return 0


def run_import(conn):
    from . import mytrips

    trips, raw = mytrips.capture()
    if not trips:
        print("No trips seen." + (" Saved what was captured to debug/." if raw else " Did My Trips load?"))
        return 1
    added = db.import_trips(conn, trips)
    print(f"Found {len(trips)} flights, added {added} new ones to your trip log.")
    return 0


def _stop_on_sigterm(*_):
    # the Stop button sends SIGTERM; exit through the `with` blocks so the browser closes
    raise SystemExit(130)


def main(argv=None):
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    p = argparse.ArgumentParser(prog="gowild", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "tomorrow", "plan", "seats", "import", "auto", "serve"])
    p.add_argument("--search", help="only this saved search (name)")
    p.add_argument("--max", type=int, default=40, help="max lookups per run (default 40)")
    p.add_argument("--max-age", type=float, default=12, help="re-check dates older than this many hours")
    p.add_argument("--headless", action="store_true", help="hide the browser window (more likely to be blocked)")
    p.add_argument("--phone", action="store_true", help="serve: also allow your paired iPhone on the same Wi-Fi")
    p.add_argument("--trip", type=int, help="trip id for 'seats' (from the Pass value page)")
    a = p.parse_args(argv)

    conn = db.connect()
    if a.command == "serve":
        from .web import create_app

        if a.phone:
            print("Phone access on: your iPhone can reach this app over Wi-Fi once paired (see the iPhone tab).")
        create_app(phone_access=a.phone).run(host="0.0.0.0" if a.phone else "127.0.0.1", port=5050, debug=False, threaded=True)
        return 0

    if a.command == "seats":
        return run_seats(conn, a.trip)
    if a.command == "import":
        return run_import(conn)

    searches = [dict(r) for r in conn.execute("SELECT * FROM searches WHERE active=1")]
    if a.search:
        searches = [s for s in searches if s["name"].lower() == a.search.lower()]
    if not searches:
        print("No saved searches yet. Add one in the web app: python -m gowild serve")
        return 1

    if a.command == "auto":
        code = run_lookups(conn, planner.plan(conn, searches, a.max_age), a.headless, a.max, offscreen=True)
        if datetime.now().hour >= 12 and code == 0:  # tomorrow's GoWild seats are open by now
            code = run_lookups(conn, planner.tomorrow_tasks(searches), a.headless, a.max, profile="browser-profile-tomorrow", offscreen=True)
        return code
    if a.command == "tomorrow":
        tasks = planner.tomorrow_tasks(searches)
        # its own browser, so it can run while a long check is going
        return run_lookups(conn, tasks, a.headless, a.max, profile="browser-profile-tomorrow")
    tasks = planner.plan(conn, searches, a.max_age)
    if a.command == "plan":
        for t in tasks:
            print(*t)
        print(f"{len(tasks)} lookups due")
        return 0
    return run_lookups(conn, tasks, a.headless, a.max)


if __name__ == "__main__":
    sys.exit(main())
