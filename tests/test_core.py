import html
import json
from datetime import date

from gowild import db, frontier, planner, rules, value
from gowild.web import create_app

FLIGHTS = {
    "journeys": [{
        "flights": [
            {"isGoWildFareEnabled": True, "goWildFare": 13.97, "goWildFareSeatsRemaining": 4,
             "standardFare": 129.0, "stopsText": "Nonstop",
             "legs": [{"flightNumber": 1234, "departureDate": "2026-11-12T07:05:00", "arrivalDate": "2026-11-12T08:20:00"}]},
            {"isGoWildFareEnabled": False, "goWildFare": 0, "standardFare": 99.0, "stopsText": "Nonstop",
             "legs": [{"flightNumber": 1240, "departureDate": "2026-11-12T18:30:00", "arrivalDate": "2026-11-12T19:45:00"}]},
        ]
    }]
}


def page_with(data):
    return f"<html><script>var FlightData = '{html.escape(json.dumps(data))}';</script></html>"


def test_parses_gowild_flights_from_embedded_json():
    status, flights = frontier.parse_results(page_with(FLIGHTS))
    assert status == "ok"
    assert len(flights) == 1
    f = flights[0]
    assert f["flight_numbers"] == ["1234"]
    assert f["gowild_price"] == 13.97 and f["gowild_seats"] == 4 and f["standard_fare"] == 129.0
    assert f["depart"].startswith("2026-11-12T07:05")


def test_parses_captured_network_json_and_unknown_pages():
    assert frontier.parse_results("<html></html>", [FLIGHTS])[0] == "ok"
    assert frontier.parse_results("<html>nothing here</html>") == ("unparsed", [])
    assert frontier.parse_results("<html>No flights available</html>") == ("no_flights", [])
    assert frontier.looks_blocked('<div id="px-captcha"></div>')


def test_search_url():
    assert frontier.search_url("DEN", "LAS", "2026-11-05") == (
        "https://booking.flyfrontier.com/Flight/InternalSelect?o1=DEN&d1=LAS&dd1=Nov%2005%2C%202026&ADT=1&mon=true&promo="
    )


def test_rules():
    assert rules.is_blackout(date(2026, 11, 25)) and not rules.is_blackout(date(2026, 11, 26))
    today = date(2026, 11, 1)
    assert not rules.is_early_booking(date(2026, 11, 2), "DEN", "LAS", today)
    assert rules.is_early_booking(date(2026, 11, 3), "DEN", "LAS", today)
    assert not rules.is_early_booking(date(2026, 11, 10), "DEN", "CUN", today)


def test_planner_skips_blackouts_weekdays_and_fresh_checks(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    s = {"origins": "DEN", "destinations": "LAS,MCO", "start_date": "2026-11-20", "end_date": "2026-11-30", "weekdays": "0123456"}
    tasks = planner.plan(conn, [s], today=date(2026, 11, 1))
    days = {t[2] for t in tasks}
    assert "2026-11-24" not in days and "2026-11-26" in days
    assert len(tasks) == 2 * len(days)
    db.save_check(conn, "DEN", "LAS", "2026-11-26", "ok", [])
    assert ("DEN", "LAS", "2026-11-26") not in planner.plan(conn, [s], today=date(2026, 11, 1))
    fridays = planner.plan(conn, [dict(s, weekdays="4")], today=date(2026, 11, 1))
    assert {t[2] for t in fridays} == {"2026-11-20", "2026-11-27"}


def test_value_summary():
    trips = [{"cash_fare": 120, "paid": 15}, {"cash_fare": 90, "paid": 20}]
    v = value.summarize(trips, 199, date(2027, 2, 28), today=date(2026, 11, 1))
    assert v["saved"] == 175 and v["remaining"] == 24 and v["trips_to_break_even"] == 1
    assert not v["broke_even"]


def test_web_pages_render(tmp_path):
    path = str(tmp_path / "w.db")
    app = create_app(path).test_client()
    assert app.get("/").status_code == 200
    r = app.post("/searches", data={"name": "Vegas", "origins": "den", "destinations": "las", "start_date": "2026-11-01",
                                    "end_date": "2027-01-31", "weekdays": ["4", "5"]})
    assert r.status_code == 302
    conn = db.connect(path)
    d = (date.today().replace(day=1)).isoformat()
    from datetime import timedelta
    future = (date.today() + timedelta(days=5)).isoformat()
    db.save_check(conn, "DEN", "LAS", future, "ok", frontier.parse_results(page_with(FLIGHTS))[1])
    page = app.get("/searches/1").get_data(as_text=True)
    assert "Vegas" in page
    assert app.get("/tomorrow").status_code == 200
    app.post("/value", data={"date": future, "origin": "den", "dest": "las", "depart_time": "07:05", "cash_fare": "129", "paid": "13.97"})
    app.post("/value", data={"action": "settings", "pass_price": "199", "tier": "gold"})
    v = app.get("/value").get_data(as_text=True)
    assert "$115" in v and "UpFront Plus upgrade" in v


def test_blackout_dates_are_opt_in(tmp_path):
    conn = db.connect(str(tmp_path / "b.db"))
    s = {"origins": "DEN", "destinations": "LAS", "start_date": "2026-11-23", "end_date": "2026-11-26", "weekdays": "0123456"}
    days = lambda s: {t[2] for t in planner.plan(conn, [s], today=date(2026, 11, 1))}
    assert days(s) == {"2026-11-23", "2026-11-26"}
    assert days(dict(s, include_blackouts=1)) == {"2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"}


def test_old_database_gets_blackout_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE searches (id INTEGER PRIMARY KEY, name TEXT NOT NULL, origins TEXT NOT NULL, destinations TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, weekdays TEXT NOT NULL DEFAULT '0123456', active INTEGER NOT NULL DEFAULT 1)")
    old.execute("INSERT INTO searches (name, origins, destinations, start_date, end_date) VALUES ('a','DEN','LAS','2026-11-01','2026-11-05')")
    old.commit(); old.close()
    row = db.connect(path).execute("SELECT include_blackouts FROM searches").fetchone()
    assert row["include_blackouts"] == 1
