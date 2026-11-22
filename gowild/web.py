"""Local web app: saved searches, GoWild calendar, tomorrow's flights, pass value."""
import calendar
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

from flask import Flask, abort, make_response, redirect, render_template, request, url_for

from . import db, notify, phone, planner, rules, seats, value
from .db import split_codes
from .frontier import DEFAULT_TABS, MAX_TABS, filter_front, search_url

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _hhmm(iso):
    return datetime.fromisoformat(iso[:16]).strftime("%-I:%M %p") if iso else "?"


def duration_txt(minutes):
    """245 -> '4h 05m'; 50 -> '50m'."""
    if minutes is None:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def days_later(start_iso, end_iso) -> int:
    """How many calendar days after start_iso's date end_iso falls (local dates)."""
    try:
        return (date.fromisoformat(end_iso[:10]) - date.fromisoformat(start_iso[:10])).days
    except (TypeError, ValueError):
        return 0


LONG_LAYOVER = 6 * 60  # Frontier flags layovers of six hours or more


def decorate_flight(f):
    """Display text for one itinerary: times, next-day arrival, total time, each leg and layover."""
    f["dep_txt"] = _hhmm(f["depart"])
    if "legs" not in f:
        # saved before the app read each leg; its arrival time came out in UTC, so don't show it
        f["arr_txt"], f["stale"] = None, True
        return f
    f["arr_txt"] = _hhmm(f["arrive"])
    f["days_later"] = days_later(f["depart"], f["arrive"])
    f["arr_day"] = datetime.fromisoformat(f["arrive"][:16]).strftime("%a, %b %-d") if f["days_later"] else None
    f["total_txt"] = duration_txt(f.get("minutes"))
    legs = f["legs"]
    stations = [legs[0]["from"]] + [l["to"] for l in legs] if legs else []
    f["route"] = " → ".join(stations) if stations and all(stations) else None
    for l in legs:
        l["dep_txt"], l["arr_txt"] = _hhmm(l["depart"]), _hhmm(l["arrive"])
        l["days_later"] = days_later(f["depart"], l["arrive"])
    for lay in f.get("layovers") or []:
        lay["txt"] = duration_txt(lay["minutes"])
        lay["long"] = (lay["minutes"] or 0) >= LONG_LAYOVER
    n = len(legs) - 1
    f["stops_txt"] = "Nonstop" if n == 0 else f"{n} stop{'s' if n > 1 else ''}"
    return f


def _decorate(check):
    """Add what the templates need to each check row."""
    d = date.fromisoformat(check["date"])
    early = rules.is_early_booking(d, check["origin"], check["dest"])
    for f in check["flights"]:
        decorate_flight(f)
    check["early"] = early and bool(check["flights"])
    check["blackout"] = rules.is_blackout(d)
    check["url"] = search_url(check["origin"], check["dest"], check["date"])
    return check


FRONT_MODES = {"": "All flights", "big": "Big front seats", "first": "First Class"}


def _front_mode(c):
    """The big-front-seat filter: from the link the user tapped, else the last one they picked."""
    mode = request.args.get("front")
    if mode is not None and mode in FRONT_MODES:
        db.set_setting(c, "front", mode)
        return mode
    mode = db.get_setting(c, "front", "")
    return mode if mode in FRONT_MODES else ""


def front_url(mode):
    """This page's URL with the front-seat filter set to mode (other filters kept)."""
    args = {k: v for k, v in request.args.items() if k != "front"}
    return url_for(request.endpoint, **(request.view_args or {}), **args, front=mode)


STOP_MODES = {"": "Any stops", "0": "Nonstop only", "1": "Up to 1 stop"}


def stop_count(f) -> int | None:
    """Stops on an itinerary: from its legs, else Frontier's 'Nonstop' / '1 Stop DEN' text,
    else its flight numbers (one per leg)."""
    if f.get("legs"):
        return len(f["legs"]) - 1
    text = str(f.get("stops") or "").lower()
    if "nonstop" in text:
        return 0
    m = re.match(r"(\d+)\s*stop", text)
    if m:
        return int(m.group(1))
    return len(f["flight_numbers"]) - 1 if f.get("flight_numbers") else None


def _stops_mode(c):
    """The stops filter: from the link the user tapped, else the last one they picked."""
    mode = request.args.get("stops")
    if mode is not None and mode in STOP_MODES:
        db.set_setting(c, "stops", mode)
        return mode
    mode = db.get_setting(c, "stops", "")
    return mode if mode in STOP_MODES else ""


def stops_url(mode):
    """This page's URL with the stops filter set to mode (other filters kept)."""
    args = {k: v for k, v in request.args.items() if k != "stops"}
    return url_for(request.endpoint, **(request.view_args or {}), **args, stops=mode)


def _apply_stops(check, mode):
    """Keep flights with at most `mode` stops; ones we can't count are kept, not hidden."""
    if mode:
        limit = int(mode)
        check["flights"] = [f for f in check["flights"] if (stop_count(f) if stop_count(f) is not None else 0) <= limit]
    return check


def _apply_front(check, mode):
    """Drop flights without the chosen front seats; remember how many we couldn't tell about."""
    check["had_gowild"] = bool(check["flights"])
    check["unknown_front"] = sum(1 for f in check["flights"] if not f.get("front_seats")) if mode else 0
    check["flights"] = filter_front(check["flights"], mode)
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
_stopped_at = {}  # kind -> when Stop was last pressed
STOP_GRACE = 5  # seconds after a Stop when a start of the same kind is taken as a stray click


def _running(kind):
    p = _jobs.get(kind)
    return p is not None and p.poll() is None


def _spawn(kind, *args):
    """Start a background run unless one of the same kind is still going, or you just
    stopped one: Stop reloads the page, and a second click (or a double-click) used to land
    on a Check now button that the reload had moved under the pointer."""
    if _running(kind) or time.time() - _stopped_at.get(kind, 0) < STOP_GRACE:
        return False
    with open(_log_path(kind), "w") as fh:
        _jobs[kind] = subprocess.Popen(
            [sys.executable, "-u", "-m", "gowild", *args], cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT
        )
    return True


def _stop(kind):
    """Ask a run to stop; it closes its browser on the way out."""
    _stopped_at[kind] = time.time()
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


AUTO_AGENT = os.path.expanduser("~/Library/LaunchAgents/local.gowild-finder.auto.plist")


def _scheduled() -> bool:
    return os.path.exists(AUTO_AGENT)


def _last_auto():
    """When an automatic check last actually searched (not a run skipped because they're off)."""
    try:
        with open(_log_path("auto")) as fh:
            text = fh.read()
        stamps = re.findall(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) Automatic check:", text, re.M)
        return datetime.fromisoformat(stamps[-1]) if stamps else None
    except OSError:
        return None


def _search_fields(f):
    """Form values for a saved search, in column order."""
    weekdays = "".join(sorted(f.getlist("weekdays"))) or "0123456"
    return (
        f["name"] or f"{f['origins']} to {f['destinations']}", ",".join(split_codes(f["origins"])),
        ",".join(split_codes(f["destinations"])), f["start_date"], f["end_date"], weekdays,
        1 if f.get("include_blackouts") else 0,
        1 if f.get("auto") == "on" else 0, int(f.get("auto_hours") or 3) if int(f.get("auto_hours") or 3) in planner.AUTO_CHOICES else 3,
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
            scheduled=_scheduled() and any(r["auto"] for r in c.execute("SELECT auto FROM searches")),
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
        return {"pass_name": rules.PASS_NAME, "today": date.today(), "is_local": is_local(), "front_url": front_url, "stops_url": stops_url, "stop_modes": STOP_MODES, "auto_choices": planner.AUTO_CHOICES}

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
        auto = {"scheduled": _scheduled(), "last": _last_auto(), "choices": planner.AUTO_CHOICES, "any": any(r["auto"] for r in rows)}
        return render_template("index.html", searches=rows, default_end=default_end, job=_job("check"), at_once=tabs, max_tabs=MAX_TABS, auto=auto)

    @app.post("/searches/<int:sid>/auto")
    def search_auto(sid):
        """Turn automatic checks on or off for one search, and how often they run."""
        f = request.form
        hours = int(f.get("auto_hours") or 3)
        c = conn()
        c.execute("UPDATE searches SET auto=?, auto_hours=? WHERE id=?",
                  (1 if f.get("auto") == "on" else 0, hours if hours in planner.AUTO_CHOICES else 3, sid))
        c.commit()
        return redirect(url_for("index") + f"#s{sid}")

    @app.post("/settings/tabs")
    def set_tabs():
        db.set_setting(conn(), "tabs", max(1, min(MAX_TABS, int(request.form.get("tabs", 1)))))
        return redirect(url_for("index") + "#settings")

    @app.post("/searches")
    def add_search():
        c = conn()
        c.execute(
            "INSERT INTO searches (name, origins, destinations, start_date, end_date, weekdays, include_blackouts, auto, auto_hours, position) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                "UPDATE searches SET name=?, origins=?, destinations=?, start_date=?, end_date=?, weekdays=?, include_blackouts=?, auto=?, auto_hours=? WHERE id=?",
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
        front, stops = _front_mode(c), _stops_mode(c)
        all_checks = [_apply_stops(_apply_front(_decorate(x), front), stops) for x in db.get_checks(c, split_codes(s["origins"]), split_codes(s["destinations"]), s["start_date"], s["end_date"])]
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
                               route_tabs=tabs, route=route, all_dates=all_dates, front=front, front_modes=FRONT_MODES, stops=stops,
                               unknown_front=sum(x["unknown_front"] for x in checks if x["date"] > future))

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

    @app.post("/trips/fares")
    def trip_fares():
        _spawn("account", "fares")
        return redirect(url_for("pass_value") + "#trips")

    @app.post("/stop/<kind>")
    def stop(kind):
        _stop(kind)
        # back to the run's card, not the top of the page where the Check now buttons are
        return redirect((request.referrer or url_for("index")).split("#")[0] + "#job")

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
        front, stops = _front_mode(c), _stops_mode(c)
        rows = [_apply_stops(_apply_front(_decorate(dict(r, flights=json.loads(r["flights"]))), front), stops) for r in c.execute("SELECT * FROM checks WHERE date=?", (t,))
                if (r["origin"], r["dest"]) in routes]
        flights = sorted(
            ({**f, "origin": r["origin"], "dest": r["dest"], "url": r["url"], "date": t} for r in rows for f in r["flights"]),
            key=lambda f: f["depart"] or "",
        )
        checked = max((r["checked_at"] for r in rows), default=None)
        empty_routes = [r for r in rows if not r["had_gowild"]]
        return render_template("tomorrow.html", day=date.fromisoformat(t), flights=flights, checked=checked, empty_routes=empty_routes, job=_job("tomorrow"),
                               searches=searches, picked={s["id"] for s in picked}, front=front, front_modes=FRONT_MODES, stops=stops,
                               unknown_front=sum(r["unknown_front"] for r in rows))

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
        db.fill_cash_fares(c)  # a check may have priced a logged trip since
        trips = [dict(t) for t in c.execute("SELECT * FROM trips ORDER BY date, depart_time")]
        now = datetime.now()
        for t in trips:
            t["saved"] = round(t["cash_fare"] - (t["paid"] or 0), 2) if t["cash_fare"] else None
            if t["depart_time"]:
                dep = datetime.fromisoformat(f"{t['date']}T{t['depart_time']}")
                t["dep_txt"] = _hhmm(dep.isoformat())
                t["upcoming"] = dep > now
                t["checkin"] = rules.checkin_opens(dep)
                t["upgrade"] = rules.upgrade_window_opens(dep, tier)
            t["seats_at"], snap = db.latest_seats(c, t["id"])
            t["seat_groups"] = seats.summarize(snap)
            if t.get("arrive"):
                t["arr_txt"] = _hhmm(t["arrive"])
                t["days_later"] = days_later(t["date"], t["arrive"])
                if t["days_later"]:
                    t["arr_day"] = datetime.fromisoformat(t["arrive"][:16]).strftime("%a, %b %-d")
        for a, b in zip(trips, trips[1:]):
            # a connection: the next flight of the same booking leaves from where this one lands, within a day
            if a.get("arrive") and b["depart_time"] and a["confirmation"] and a["confirmation"] == b["confirmation"] and a["dest"] == b["origin"]:
                gap = round((datetime.fromisoformat(f"{b['date']}T{b['depart_time']}") - datetime.fromisoformat(a["arrive"][:16])).total_seconds() / 60)
                if 0 <= gap < 24 * 60:
                    a["layover"] = {"airport": a["dest"], "txt": duration_txt(gap), "long": gap >= LONG_LAYOVER,
                                    "overnight": b["date"] > a["arrive"][:10]}
        summary = value.summarize(trips, price, rules.PASS_END)
        return render_template("value.html", trips=trips, v=summary, price=price, tier=tier, prefill=request.args, job=_job("account"))

    return app
