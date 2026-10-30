"""Command line: python -m gowild <command>

  check      run due lookups for your saved searches (slowly, logged out)
  tomorrow   check tomorrow (the regular GoWild window) for the searches picked on the Tomorrow page
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
from .frontier import DEFAULT_TABS, MAX_TABS, Blocked, FrontierBrowser


def run_lookups(conn, tasks, headless, limit, profile="browser-profile", offscreen=False, tabs=None):
    tasks = tasks[:limit]
    tabs = max(1, min(MAX_TABS, tabs or int(db.get_setting(conn, "tabs", DEFAULT_TABS))))
    print(f"{len(tasks)} lookups to run" + (f", {tabs} at a time" if tabs > 1 and len(tasks) > 1 else ""), flush=True)
    if not tasks:
        return 0
    found, finds, done = 0, [], 0

    def alert():
        if finds and notify.send(conn, *notify.new_finds_message(finds)):
            print(f"Sent an alert for {len(finds)} new date(s).", flush=True)

    with FrontierBrowser(headless=headless, profile=profile, offscreen=offscreen, tabs=tabs) as br:
        for n in range(0, len(tasks), br.tabs):
            blocked = None
            for (o, d, day), status, flights, err in br.lookup_many(tasks[n:n + br.tabs]):
                if isinstance(err, Blocked):
                    blocked = blocked or err
                    db.save_check(conn, o, d, day, "blocked", [])
                    continue
                if err and "has been closed" in str(err):  # someone closed the window: treat it as Stop
                    print("The browser window was closed, so the check stopped. What it found so far is saved.", flush=True)
                    alert()
                    return 3
                if err:  # keep going on one bad page
                    print(f"  error: {err}", flush=True)
                if db.save_check(conn, o, d, day, status, flights):
                    finds.append((o, d, day, len(flights), rules.is_early_booking(date.fromisoformat(day), o, d)))
                found += len(flights)
                done += 1
                print(f"[{done}/{len(tasks)}] {o}-{d} {day}: {status}, {len(flights)} GoWild", flush=True)
            if blocked:
                tip = " Set searches at once back to 1 on the Saved searches page." if br.tabs > 1 else ""
                print(f"Stopping: {blocked}. Try again later, or run without --headless.{tip}", flush=True)
                alert()
                return 2
    print(f"Done. {found} GoWild flights found.", flush=True)
    alert()
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
    p.add_argument("--ids", help="only these saved searches, by id (e.g. 1,3)")
    p.add_argument("--max", type=int, default=40, help="max lookups per run (default 40)")
    p.add_argument("--max-age", type=float, default=12, help="re-check dates older than this many hours")
    p.add_argument("--tabs", type=int, choices=range(1, MAX_TABS + 1), help="searches to load at once (default: the app's setting)")
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
    all_searches = searches
    if a.ids:
        ids = {int(i) for i in a.ids.split(",") if i.strip()}
        searches = [s for s in searches if s["id"] in ids]
    if not searches:
        print("No saved searches yet. Add one in the web app: python -m gowild serve")
        return 1

    if a.command == "auto":
        code = run_lookups(conn, planner.plan(conn, searches, a.max_age), a.headless, a.max, offscreen=True, tabs=a.tabs)
        if datetime.now().hour >= 12 and code == 0:  # tomorrow's GoWild seats are open by now
            picked = db.tomorrow_searches(conn, all_searches)
            code = run_lookups(conn, planner.tomorrow_tasks(picked), a.headless, a.max, profile="browser-profile-tomorrow", offscreen=True, tabs=a.tabs)
        return code
    if a.command == "tomorrow":
        tasks = planner.tomorrow_tasks(searches if a.ids else db.tomorrow_searches(conn, searches))
        # its own browser, so it can run while a long check is going
        return run_lookups(conn, tasks, a.headless, a.max, profile="browser-profile-tomorrow", tabs=a.tabs)
    tasks = planner.plan(conn, searches, a.max_age)
    if a.command == "plan":
        for t in tasks:
            print(*t)
        print(f"{len(tasks)} lookups due")
        return 0
    return run_lookups(conn, tasks, a.headless, a.max, tabs=a.tabs)


if __name__ == "__main__":
    sys.exit(main())
