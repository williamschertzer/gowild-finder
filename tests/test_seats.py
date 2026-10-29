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
    assert "UpFront Plus: 1 open, $89" in page
    assert "Premium: 2 open, $45–$52" in page
    assert "Seat prices" in page
