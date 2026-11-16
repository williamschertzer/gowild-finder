from datetime import date, timedelta

from gowild import db, seats
from gowild.web import create_app

SEATMAP = {"seatMap": {"decks": [{"rows": [
    {"seats": [{"unitDesignator": "1A", "availability": "Open", "seatType": "UpFront Plus", "price": {"amount": 89.0}},
               {"unitDesignator": "1C", "availability": "Taken", "seatType": "UpFront Plus", "price": {"amount": 89.0}}]},
    {"seats": [{"unitDesignator": "3D", "availability": "Open", "seatType": "Premium Seating", "price": {"amount": 45.0}},
               {"unitDesignator": "3F", "availability": "Open", "seatType": "Premium Seating", "price": {"amount": 52.5}}]},
    {"seats": [{"unitDesignator": "20B", "availability": "Open", "seatType": "Standard", "price": {"amount": 21.0}}]},
]}]}}


def test_parse_and_summarize_seat_map():
    found = seats.parse_seats([SEATMAP])
    assert [s["seat"] for s in found] == ["1A", "1C", "3D", "3F", "20B"]
    assert not next(s for s in found if s["seat"] == "1C")["available"]
    groups = seats.summarize(found)
    assert [g["type"] for g in groups] == ["UpFront Plus", "Premium", "Standard"]
    assert groups[0]["open"] == 1 and groups[0]["min"] == 89.0
    assert (groups[1]["min"], groups[1]["max"]) == (45.0, 52.5)


def test_ignores_json_without_seats():
    assert seats.parse_seats([{"flight": "1234", "fare": 99}]) == []


def test_value_page_shows_seat_prices(tmp_path):
    path = str(tmp_path / "s.db")
    client = create_app(path).test_client()
    d = (date.today() + timedelta(days=3)).isoformat()
    client.post("/value", data={"date": d, "origin": "DEN", "dest": "LAS", "depart_time": "07:05", "cash_fare": "129", "paid": "14"})
    db.save_seats(db.connect(path), 1, seats.parse_seats([SEATMAP]))
    page = client.get("/value").get_data(as_text=True)
    assert "UpFront Plus 1 · $89" in page
    assert "Premium 2 · $45–52" in page
    assert "Seat prices" in page


# ---------------------------------------------------------------- big front seats on search results

def _flight(**extra):
    return {"isGoWildFareEnabled": True, "goWildFare": 13.97, "stopsText": "Nonstop",
            "legs": [{"flightNumber": 1234, "departureDate": "2027-03-10T07:05:00", "arrivalDate": "2027-03-10T08:20:00"}], **extra}


def test_front_seats_detected_from_fare_and_bundle_data():
    from gowild import frontier
    assert frontier.parse_flight(_flight())["front_seats"] is None
    assert frontier.parse_flight(_flight(bundles=[{"name": "UpFront Plus", "price": 59}]))["front_seats"] == "UpFront Plus"
    assert frontier.parse_flight(_flight(hasUpFrontPlus=True))["front_seats"] == "UpFront Plus"
    assert frontier.parse_flight(_flight(hasUpFrontPlus=False))["front_seats"] is None
    assert frontier.parse_flight(_flight(firstClassFare=0))["front_seats"] is None
    both = _flight(bundles=[{"name": "First Class Bundle"}, {"name": "UpFront Plus"}])
    assert frontier.parse_flight(both)["front_seats"] == "First Class"
    assert frontier.parse_flight(_flight(equipmentType="A321neo"))["aircraft"] == "A321neo"


def test_filter_front_modes():
    from gowild.frontier import filter_front
    fl = [{"front_seats": "First Class"}, {"front_seats": "UpFront Plus"}, {"front_seats": None}]
    assert filter_front(fl, "") == fl
    assert filter_front(fl, "big") == fl[:2]
    assert filter_front(fl, "first") == fl[:1]


def test_front_filter_on_search_and_tomorrow_pages(tmp_path):
    import json
    from datetime import date, timedelta
    from gowild import db
    from gowild.web import create_app
    path = str(tmp_path / "t.db")
    c = db.connect(path)
    d = (date.today() + timedelta(days=1)).isoformat()
    c.execute("INSERT INTO searches (name, origins, destinations, start_date, end_date) VALUES ('t','DEN','LAS',?,?)", (d, d))
    c.commit()
    flights = [
        {"depart": d + "T07:00", "arrive": d + "T08:00", "flight_numbers": ["111"], "gowild": True, "gowild_price": 14.0, "front_seats": "First Class"},
        {"depart": d + "T09:00", "arrive": d + "T10:00", "flight_numbers": ["222"], "gowild": True, "gowild_price": 14.0, "front_seats": None},
    ]
    db.save_check(c, "DEN", "LAS", d, "ok", flights)
    db.set_setting(c, "tomorrow_ids", "1")
    app = create_app(path).test_client()
    page = app.get("/searches/1").get_data(as_text=True)
    assert "111" in page and "222" in page and "First Class seats" in page
    page = app.get("/searches/1?front=first").get_data(as_text=True)
    assert "111" in page and "222" not in page and "1 flight hidden" in page
    page = app.get("/tomorrow").get_data(as_text=True)  # choice is remembered
    assert "111" in page and "222" not in page
    page = app.get("/tomorrow?front=").get_data(as_text=True)
    assert "222" in page
