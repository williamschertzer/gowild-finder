"""Local web app: saved searches, GoWild calendar, tomorrow's flights, pass value."""
import calendar
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

from flask import Flask, abort, make_response, redirect, render_template, request, url_for

from . import db, notify, phone, rules, seats, value
from .db import split_codes
from .frontier import DEFAULT_TABS, MAX_TABS, search_url

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _hhmm(iso):
    return datetime.fromisoformat(iso[:16]).strftime("%-I:%M %p") if iso else "?"


def _decorate(check):
    """Add what the templates need to each check row."""
    d = date.fromisoformat(check["date"])
    early = rules.is_early_booking(d, check["origin"], check["dest"])
    for f in check["flights"]:
        f["dep_txt"], f["arr_txt"] = _hhmm(f["depart"]), _hhmm(f["arrive"])
    check["early"] = early and bool(check["flights"])
    check["blackout"] = rules.is_blackout(d)
    check["url"] = search_url(check["origin"], check["dest"], check["date"])
    return check


def month_grid(start: date, end: date, by_date: dict):
    """Calendar months covering start..end; each day says what we know about it."""
    months, cur = [], date(start.year, start.month, 1)
    today = date.today()
    while cur <= end:
        weeks = []
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(cur.year, cur.month):
            row = []
            for d in week:
                if d.month != cur.month:
                    row.append(None)
                    continue
                checks = by_date.get(d.isoformat(), [])
                n = sum(len(c["flights"]) for c in checks)
                if d < start or d > end or d <= today:
                    state = "out"
                elif rules.is_blackout(d):
                    state = "blackout-gw" if n else "blackout"  # GoWild on a blackout day needs the Peak Day charge
                elif not checks:
                    state = "unchecked"
                elif n:
                    state = "early" if any(c["early"] for c in checks) else "gowild"
                elif any(c["status"] in ("blocked", "error", "unparsed") for c in checks):
                    state = "problem"
                else:
                    state = "none"
                row.append({"day": d.day, "iso": d.isoformat(), "state": state, "n": n})
            weeks.append(row)
        months.append({"title": cur.strftime("%B %Y"), "weeks": weeks})
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return months


_jobs = {}  # kind -> Popen; one of each kind at a time


def _running(kind):
    p = _jobs.get(kind)
    return p is not None and p.poll() is None


def _spawn(kind, *args):
    """Start a background run unless one of the same kind is still going."""
    if _running(kind):
        return False
    with open(_log_path(kind), "w") as fh:
        _jobs[kind] = subprocess.Popen(
            [sys.executable, "-u", "-m", "gowild", *args], cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT
        )
    return True


def _stop(kind):
    """Ask a run to stop; it closes its browser on the way out."""
    p = _jobs.get(kind)
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
        with open(_log_path(kind), "a") as fh:
            fh.write("\nStopped.\n")
        return True
    return False


def _log_path(kind):
    return os.path.join(ROOT, f"last-{kind}.log")


def _job(kind):
    """What the page shows about the latest run of this kind."""
    try:
        with open(_log_path(kind)) as fh:
            text = fh.read()
    except OSError:
        text = ""
    lines = text.splitlines()
    # Playwright's launch dump is noise unless something failed
    if "Traceback" not in text and "Error" not in text:
        lines = [l for l in lines if not l.startswith(("  - ", "    - "))]
    return {"kind": kind, "running": _running(kind), "log": "\n".join(lines[-25:])}


def _search_fields(f):
    """Form values for a saved search, in column order."""
    weekdays = "".join(sorted(f.getlist("weekdays"))) or "0123456"
    return (
        f["name"] or f"{f['origins']} to {f['destinations']}", ",".join(split_codes(f["origins"])),
        ",".join(split_codes(f["destinations"])), f["start_date"], f["end_date"], weekdays,
        1 if f.get("include_blackouts") else 0,
    )


def create_app(db_path=None, phone_access=False):
    app = Flask(__name__)
    app.config["PHONE_ACCESS"] = phone_access

    def conn():
        return db.connect(db_path)

    def is_local():
        return request.remote_addr in ("127.0.0.1", "::1")

    @app.before_request
    def require_key():
        """The Mac itself is trusted; other devices (your iPhone) need the pairing key."""
        if is_local() or request.endpoint in ("pair", "static"):
            return None
        if request.cookies.get(phone.COOKIE) != phone.key(conn()):
            return make_response(render_template("locked.html"), 403)

    @app.get("/pair")
    def pair():
        if request.args.get("key") != phone.key(conn()):
            abort(403)
        resp = redirect(url_for("tomorrow"))
        resp.set_cookie(phone.COOKIE, request.args["key"], max_age=400 * 24 * 3600, httponly=True, samesite="Lax")
        return resp

    @app.route("/phone", methods=["GET", "POST"], endpoint="phone")
    def phone_page():
        if not is_local():
            abort(403)
        c = conn()
        if request.method == "POST":  # new key: phones paired before must scan again
            db.set_setting(c, "phone_key", "")
            return redirect(url_for("phone"))
        k = phone.key(c)
        hosts = phone.lan_addresses()
        urls = [phone.pair_url(h, k) for h in hosts]
        listening = app.config.get("PHONE_ACCESS", False)
        t = notify.topic(c)
        ts = phone.tailscale_address()
        ts_url = phone.pair_url(ts, k) if ts else None
        return render_template(
            "phone.html", urls=urls, qr=phone.qr_svg(urls[0]) if urls else None, listening=listening,
            alerts=notify.enabled(c), topic=t, topic_url=f"{notify.SERVER}/{t}",
            scheduled=os.path.exists(os.path.expanduser("~/Library/LaunchAgents/local.gowild-finder.auto.plist")),
            sent=request.args.get("sent"), ts_url=ts_url, ts_qr=phone.qr_svg(ts_url) if ts_url else None,
        )

    @app.post("/alerts")
    def alerts():
        if not is_local():
            abort(403)
        c = conn()
        action = request.form.get("action")
        if action in ("on", "off"):
            db.set_setting(c, "alerts", action)
            return redirect(url_for("phone") + "#alerts")
        ok = notify.send(c, "GoWild Finder test", "Alerts are working. You'll get one when new GoWild dates show up.", force=True)
        return redirect(url_for("phone", sent="ok" if ok else "fail") + "#alerts")

    @app.context_processor
    def globals_():
        return {"pass_name": rules.PASS_NAME, "today": date.today(), "is_local": is_local()}

    @app.get("/")
    def index():
        c = conn()
        rows = []
        for s in db.ordered_searches(c):
            checks = [_decorate(x) for x in db.get_checks(c, split_codes(s["origins"]), split_codes(s["destinations"]), s["start_date"], s["end_date"])]
            hit_dates = {x["date"] for x in checks if x["flights"]}
            s["gowild_dates"] = len(hit_dates)
            s["early_dates"] = len({x["date"] for x in checks if x["early"]})
            s["next"] = min((d for d in hit_dates if d > date.today().isoformat()), default=None)
            rows.append(s)
        default_end = min(date.today() + timedelta(days=60), rules.PASS_END)
        tabs = int(db.get_setting(c, "tabs", DEFAULT_TABS))
        return render_template("index.html", searches=rows, default_end=default_end, job=_job("check"), at_once=tabs, max_tabs=MAX_TABS)

    @app.post("/settings/tabs")
    def set_tabs():
        db.set_setting(conn(), "tabs", max(1, min(MAX_TABS, int(request.form.get("tabs", 1)))))
        return redirect(url_for("index"))

    @app.post("/searches")
    def add_search():
        c = conn()
        c.execute(
            "INSERT INTO searches (name, origins, destinations, start_date, end_date, weekdays, include_blackouts, position) VALUES (?,?,?,?,?,?,?,?)",
            (*_search_fields(request.form), db.top_position(c)),
        )
        c.commit()
        return redirect(url_for("index"))

    @app.route("/searches/<int:sid>/edit", methods=["GET", "POST"])
    def edit_search(sid):
        c = conn()
        row = c.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone()
        if not row:
            abort(404)
        if request.method == "POST":
            c.execute(
                "UPDATE searches SET name=?, origins=?, destinations=?, start_date=?, end_date=?, weekdays=?, include_blackouts=? WHERE id=?",
                (*_search_fields(request.form), sid),
            )
            c.commit()
            return redirect(url_for("show_search", sid=sid))
        return render_template("edit_search.html", s=dict(row))

    @app.post("/searches/<int:sid>/copy")
    def copy_search(sid):
        c = conn()
        row = c.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone()
        if not row:
            abort(404)
        cur = c.execute(
            "INSERT INTO searches (name, origins, destinations, start_date, end_date, weekdays, include_blackouts, position) VALUES (?,?,?,?,?,?,?,?)",
            (f"{row['name']} (copy)", row["origins"], row["destinations"], row["start_date"], row["end_date"], row["weekdays"], row["include_blackouts"], db.top_position(c)),
        )
        c.commit()
        return redirect(url_for("edit_search", sid=cur.lastrowid))

    @app.post("/searches/<int:sid>/move/<direction>")
    def move_search(sid, direction):
        db.move_search(conn(), sid, -1 if direction == "up" else 1)
        return redirect(url_for("index") + f"#s{sid}")

    @app.post("/searches/<int:sid>/delete")
    def delete_search(sid):
        c = conn()
        c.execute("DELETE FROM searches WHERE id=?", (sid,))
        c.commit()
        _stop("check")  # its lookups were planned when it started, so don't let it carry on
        return redirect(url_for("index"))

    @app.get("/searches/<int:sid>")
    def show_search(sid):
        c = conn()
        s = dict(c.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone())
        all_checks = [_decorate(x) for x in db.get_checks(c, split_codes(s["origins"]), split_codes(s["destinations"]), s["start_date"], s["end_date"])]
        future = date.today().isoformat()
        # one tab per airport pair, plus "All", when the search has more than one pair
        routes = [(o, d) for o in split_codes(s["origins"]) for d in split_codes(s["destinations"]) if o != d]
        tabs = [{
            "key": f"{o}-{d}", "label": f"{o} → {d}",
            "dates": len({x["date"] for x in all_checks if (x["origin"], x["dest"]) == (o, d) and x["flights"] and x["date"] > future}),
        } for o, d in routes] if len(routes) > 1 else []
        route = request.args.get("route")
        checks = [x for x in all_checks if f"{x['origin']}-{x['dest']}" == route] if route and any(t["key"] == route for t in tabs) else all_checks
        if checks is all_checks:
            route = None
        by_date = {}
        for x in checks:
            by_date.setdefault(x["date"], []).append(x)
        start, end = date.fromisoformat(s["start_date"]), min(date.fromisoformat(s["end_date"]), rules.PASS_END)
        hits = [x for x in checks if x["flights"] and x["date"] > future]
        all_dates = len({x["date"] for x in all_checks if x["flights"] and x["date"] > future})
        return render_template("search.html", s=s, months=month_grid(start, end, by_date), hits=hits, job=_job("check"),
                               route_tabs=tabs, route=route, all_dates=all_dates)

    @app.post("/run/<kind>")
    def run(kind):
        if kind == "tomorrow":
            ids = ",".join(sorted(request.form.getlist("ids"), key=int))
            db.set_setting(conn(), "tomorrow_ids", ids)  # remembered for next time and for automatic checks
            if ids:
                _spawn("tomorrow", "tomorrow", "--ids", ids)
            return redirect(url_for("tomorrow"))
        args = ["check"] + (["--search", request.form["search"]] if request.form.get("search") else [])
        _spawn("check", *args)
        return redirect(request.referrer or url_for("index"))

    @app.post("/trips/<int:tid>/seats")
    def trip_seats(tid):
        _spawn("account", "seats", "--trip", str(tid))
        return redirect(url_for("pass_value") + "#trips")

    @app.post("/trips/import")
    def import_trips():
        _spawn("account", "import")
        return redirect(url_for("pass_value") + "#trips")

    @app.post("/stop/<kind>")
    def stop(kind):
        _stop(kind)
        return redirect(request.referrer or url_for("index"))

    @app.get("/job/<kind>")
    def job_status(kind):
        if kind not in ("check", "tomorrow", "account"):
            abort(404)
        return _job(kind)

    @app.get("/tomorrow")
    def tomorrow():
        c = conn()
        t = (date.today() + timedelta(days=1)).isoformat()
        searches = db.ordered_searches(c)
        picked = db.tomorrow_searches(c, searches)
        routes = {(o, d) for s in picked for o in split_codes(s["origins"]) for d in split_codes(s["destinations"])}
        rows = [_decorate(dict(r, flights=json.loads(r["flights"]))) for r in c.execute("SELECT * FROM checks WHERE date=?", (t,))
                if (r["origin"], r["dest"]) in routes]
        flights = sorted(
            ({**f, "origin": r["origin"], "dest": r["dest"], "url": r["url"], "date": t} for r in rows for f in r["flights"]),
            key=lambda f: f["depart"] or "",
        )
        checked = max((r["checked_at"] for r in rows), default=None)
        empty_routes = [r for r in rows if not r["flights"]]
        return render_template("tomorrow.html", day=date.fromisoformat(t), flights=flights, checked=checked, empty_routes=empty_routes, job=_job("tomorrow"),
                               searches=searches, picked={s["id"] for s in picked})

    @app.route("/value", methods=["GET", "POST"])
    def pass_value():
        c = conn()
        if request.method == "POST":
            f = request.form
            if f.get("action") == "settings":
                db.set_setting(c, "pass_price", f["pass_price"])
                db.set_setting(c, "tier", f["tier"])
            elif f.get("action") == "update":
                c.execute("UPDATE trips SET cash_fare=?, paid=? WHERE id=?", (float(f.get("cash_fare") or 0), float(f.get("paid") or 0), f["id"]))
                c.commit()
            elif f.get("action") == "delete":
                c.execute("DELETE FROM trips WHERE id=?", (f["id"],))
                c.commit()
            else:
                c.execute(
                    "INSERT INTO trips (date, depart_time, origin, dest, flight_no, cash_fare, paid, confirmation, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f["date"], f.get("depart_time") or None, f["origin"].upper(), f["dest"].upper(), f.get("flight_no"),
                     float(f.get("cash_fare") or 0), float(f.get("paid") or 0), f.get("confirmation"), f.get("notes")),
                )
                c.commit()
            return redirect(url_for("pass_value"))
        price = float(db.get_setting(c, "pass_price", rules.PASS_PRICE))
        tier = db.get_setting(c, "tier", "none")
        trips = [dict(t) for t in c.execute("SELECT * FROM trips ORDER BY date, depart_time")]
        now = datetime.now()
        for t in trips:
            t["saved"] = round((t["cash_fare"] or 0) - (t["paid"] or 0), 2)
            if t["depart_time"]:
                dep = datetime.fromisoformat(f"{t['date']}T{t['depart_time']}")
                t["upcoming"] = dep > now
                t["checkin"] = rules.checkin_opens(dep)
                t["upgrade"] = rules.upgrade_window_opens(dep, tier)
            t["seats_at"], snap = db.latest_seats(c, t["id"])
            t["seat_groups"] = seats.summarize(snap)
        summary = value.summarize(trips, price, rules.PASS_END)
        return render_template("value.html", trips=trips, v=summary, price=price, tier=tier, prefill=request.args, job=_job("account"))

    return app
