"""Command line: python -m gowild <command>

  check      run due lookups for your saved searches (slowly, logged out)
  tomorrow   check every saved route for tomorrow (the regular GoWild window)
  plan       show what 'check' would look up, without opening a browser
  serve      open the web app at http://127.0.0.1:5050
"""
import argparse
import sys

from . import db, planner
from .frontier import Blocked, FrontierBrowser


def run_lookups(conn, tasks, headless, limit):
    tasks = tasks[:limit]
    print(f"{len(tasks)} lookups to run")
    if not tasks:
        return 0
    found = 0
    with FrontierBrowser(headless=headless) as br:
        for i, (o, d, day) in enumerate(tasks, 1):
            try:
                status, flights = br.lookup(o, d, day)
            except Blocked as e:
                print(f"Stopping: {e}. Try again later, or run without --headless.")
                db.save_check(conn, o, d, day, "blocked", [])
                return 2
            except Exception as e:  # keep going on one bad page
                status, flights = "error", []
                print(f"  error: {e}")
            db.save_check(conn, o, d, day, status, flights)
            found += len(flights)
            print(f"[{i}/{len(tasks)}] {o}-{d} {day}: {status}, {len(flights)} GoWild")
    print(f"Done. {found} GoWild flights found.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="gowild", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "tomorrow", "plan", "serve"])
    p.add_argument("--search", help="only this saved search (name)")
    p.add_argument("--max", type=int, default=40, help="max lookups per run (default 40)")
    p.add_argument("--max-age", type=float, default=12, help="re-check dates older than this many hours")
    p.add_argument("--headless", action="store_true", help="hide the browser window (more likely to be blocked)")
    a = p.parse_args(argv)

    conn = db.connect()
    if a.command == "serve":
        from .web import create_app

        create_app().run(host="127.0.0.1", port=5050, debug=False)
        return 0

    searches = [dict(r) for r in conn.execute("SELECT * FROM searches WHERE active=1")]
    if a.search:
        searches = [s for s in searches if s["name"].lower() == a.search.lower()]
    if not searches:
        print("No saved searches yet. Add one in the web app: python -m gowild serve")
        return 1

    if a.command == "tomorrow":
        tasks = planner.tomorrow_tasks(searches)
        return run_lookups(conn, tasks, a.headless, a.max)
    tasks = planner.plan(conn, searches, a.max_age)
    if a.command == "plan":
        for t in tasks:
            print(*t)
        print(f"{len(tasks)} lookups due")
        return 0
    return run_lookups(conn, tasks, a.headless, a.max)


if __name__ == "__main__":
    sys.exit(main())
