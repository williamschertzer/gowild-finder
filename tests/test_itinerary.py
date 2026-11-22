"""Trip import from a trip's details page, and itinerary details (layovers, total time, next day)."""
import html
import json

from datetime import date

from gowild import db, frontier, mytrips, value
from gowild.web import create_app, decorate_flight

# trimmed from a real booking.flyfrontier.com/Booking/Index page (names and codes changed)
TRIP_PAGE = """<html><script>window.dataLayer = window.dataLayer || [];
var productInfo = {"FlightSegments":[{"flight_number":"F94277","origin":"IAD","destination":"LAS","departure_date":"2026-11-28"}]};
var totalPassengers = 1; var triptype = "OneWay"; var totalPrice = 350.19;</script>
<script>window.NOIBUJS.addCustomAttribute('PNR', 'ABC123');</script>
<input type="checkbox" value="F9~4277~ ~~IAD~11/28/2026 13:09~LAS~11/28/2026 15:31~~^F9~1923~ ~~LAS~11/28/2026 18:29~ONT~11/28/2026 19:36~~" name="x">
<input type="hidden" name="rl" value="ABC123" /></html>"""


def test_trip_details_page_is_imported():
    trips = mytrips.parse_pages([("https://booking.flyfrontier.com/Booking/Index", TRIP_PAGE)])
    assert [(t["origin"], t["dest"], t["flight_no"], t["depart_time"], t["arrive"]) for t in trips] == [
        ("IAD", "LAS", "4277", "13:09", "2026-11-28T15:31"), ("LAS", "ONT", "1923", "18:29", "2026-11-28T19:36")]
    assert all(t["confirmation"] == "ABC123" and t["paid"] == 175.09 for t in trips)


def test_pages_without_a_booking_are_ignored():
    search = "<html>F9~3215~ ~~ATL~12/12/2026 09:23~LAX~12/12/2026 11:32~~</html>"
    assert mytrips.parse_pages([("https://booking.flyfrontier.com/Flight/Select", search)]) == []


def test_imported_trips_show_layover_and_arrival(tmp_path):
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    assert db.import_trips(conn, mytrips.parse_pages([("u", TRIP_PAGE)])) == 2
    page = create_app(path).test_client().get("/value").get_data(as_text=True)
    assert "1:09 PM → 3:31 PM" in page
    assert "2h 58m layover in LAS" in page


CONNECTING = {"journeys": [{"flights": [{
    "isGoWildFareEnabled": True, "goWildFare": 157.96, "standardFare": 92.96, "stopsText": "1 Stop IAH",
    "legs": [
        {"flightNumber": 1403, "departureStation": "ATL", "arrivalStation": "IAH",
         "departureDate": "2026-12-12T19:12:00", "arrivalDate": "2026-12-12T20:40:00",
         "departureDateUtc": "2026-12-13T00:12:00Z", "arrivalDateUtc": "2026-12-13T02:40:00Z"},
        {"flightNumber": 1755, "departureStation": "IAH", "arrivalStation": "LAX",
         "departureDate": "2026-12-13T06:10:00", "arrivalDate": "2026-12-13T07:40:00",
         "departureDateUtc": "2026-12-13T12:10:00Z", "arrivalDateUtc": "2026-12-13T15:40:00Z"},
    ]}]}]}


def test_legs_layovers_total_time_and_local_arrival():
    (f,) = frontier.find_flights([CONNECTING])
    assert f["depart"] == "2026-12-12T19:12" and f["arrive"] == "2026-12-13T07:40"  # local, not the UTC 15:40
    assert f["minutes"] == 15 * 60 + 28
    assert f["layovers"] == [{"airport": "IAH", "minutes": 9 * 60 + 30, "overnight": True}]
    d = decorate_flight(f)
    assert d["total_txt"] == "15h 28m" and d["days_later"] == 1 and d["arr_day"] == "Sun, Dec 13"
    assert d["route"] == "ATL → IAH → LAX" and d["stops_txt"] == "1 stop"
    assert d["layovers"][0]["long"]


def test_flight_card_shows_next_day_layover_and_total(tmp_path):
    path = str(tmp_path / "f.db")
    conn = db.connect(path)
    flights = frontier.find_flights([CONNECTING])
    db.save_check(conn, "ATL", "LAX", "2026-12-12", "ok", flights)
    client = create_app(path).test_client()
    client.post("/searches", data={"name": "LA", "origins": "ATL", "destinations": "LAX", "start_date": "2026-12-01",
                                   "end_date": "2026-12-31", "weekdays": list("0123456")})
    page = client.get("/searches/1").get_data(as_text=True)
    assert "+1 day · lands Sun, Dec 13" in page
    assert "15h 28m total" in page
    assert "9h 30m layover in IAH" in page and "overnight" in page


def test_old_checks_without_legs_hide_the_utc_arrival():
    f = decorate_flight({"depart": "2026-12-12T15:25:00", "arrive": "2026-12-13T01:32:00", "flight_numbers": ["4315"]})
    assert f["arr_txt"] is None and f["stale"]


def test_utc_keys_do_not_leak_into_fallback_times():
    blob = {"isGoWildFareEnabled": True, "goWildFare": 50, "flightNumber": 1,
            "departureDate": "2026-12-12T09:23:00", "arrivalDate": "2026-12-12T11:32:00",
            "arrivalDateUtc": "2026-12-12T19:32:00Z"}
    (f,) = frontier.find_flights([blob])
    assert f["arrive"].startswith("2026-12-12T11:32")


def page_with(data):
    return f"<html><script>var FlightData = '{html.escape(json.dumps(data))}';</script></html>"


def test_embedded_page_round_trip():
    status, flights = frontier.parse_results(page_with(CONNECTING))
    assert status == "ok" and flights[0]["flight_numbers"] == ["1403", "1755"]


def test_connecting_trip_priced_as_one_journey_and_paid_excludes_extras(tmp_path):
    conn = db.connect(str(tmp_path / "v.db"))
    # the check saw the whole itinerary IAD -> ONT, priced as one fare
    db.save_check(conn, "IAD", "ONT", "2026-11-28", "ok", [
        {"flight_numbers": ["1923", "4277"], "standard_fare": 523.98, "gowild_price": 232.19}])
    page = TRIP_PAGE.replace("</html>", '<script>var attrs = {"BaseFare":112.5500,"TaxRevenue":119.6400};</script></html>')
    trips = mytrips.parse_pages([("u", page)])
    assert [t["paid"] for t in trips] == [116.09, 116.09]  # fare + taxes, not the $350.19 total with bags
    assert db.import_trips(conn, trips) == 2
    rows = [dict(r) for r in conn.execute("SELECT * FROM trips")]
    assert [r["cash_fare"] for r in rows] == [261.99, 261.99] and all(r["notes"] == "imported" for r in rows)
    v = value.summarize(rows, 250, date(2027, 2, 28), today=date(2026, 11, 1))
    assert v["saved"] == 291.8 and v["broke_even"] and v["unpriced"] == 0
    assert v["trips"] == 1 and v["avg_saving"] == 291.8  # two flights, one connecting trip


def test_trips_without_a_cash_fare_are_not_counted_as_losses():
    trips = [{"cash_fare": 0, "paid": 175}, {"cash_fare": 120, "paid": 20}]
    v = value.summarize(trips, 250, date(2027, 2, 28), today=date(2026, 11, 1))
    assert v["saved"] == 100 and v["remaining"] == 150 and v["unpriced"] == 1 and v["avg_saving"] == 100


def test_missing_fares_are_looked_up_by_journey(tmp_path):
    conn = db.connect(str(tmp_path / "m.db"))
    db.import_trips(conn, mytrips.parse_pages([("u", TRIP_PAGE)]))
    assert db.trips_missing_fares(conn, "2026-11-01") == [("IAD", "ONT", "2026-11-28")]
    assert db.trips_missing_fares(conn, "2026-12-01") == []  # past trips can't be searched


def test_stops_filter_on_a_search(tmp_path):
    path = str(tmp_path / "st.db")
    conn = db.connect(path)
    nonstop = {"depart": "2026-12-12T09:23", "arrive": "2026-12-12T11:32", "flight_numbers": ["3215"], "gowild": True,
               "gowild_price": 98.99, "legs": [{"flight": "3215", "from": "ATL", "to": "LAX", "depart": "2026-12-12T09:23",
                                                "arrive": "2026-12-12T11:32"}], "layovers": [], "minutes": 309}
    db.save_check(conn, "ATL", "LAX", "2026-12-12", "ok", frontier.find_flights([CONNECTING]) + [nonstop])
    client = create_app(path).test_client()
    client.post("/searches", data={"name": "LA", "origins": "ATL", "destinations": "LAX", "start_date": "2026-12-01",
                                   "end_date": "2026-12-31", "weekdays": list("0123456")})
    every = client.get("/searches/1").get_data(as_text=True)
    assert "F9 3215" in every and "layover in IAH" in every and "Nonstop only" in every
    direct = client.get("/searches/1?stops=0").get_data(as_text=True)
    assert "F9 3215" in direct and "layover in IAH" not in direct
    assert "layover in IAH" not in client.get("/searches/1").get_data(as_text=True)  # remembered
    assert "layover in IAH" in client.get("/searches/1?stops=1").get_data(as_text=True)
    assert "layover in IAH" in client.get("/searches/1?stops=").get_data(as_text=True)
