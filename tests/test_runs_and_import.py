import os
import subprocess
import sys

import pytest

from gowild import browser, db, mytrips
from gowild.web import create_app

MYTRIPS = {"bookings": [
    {"recordLocator": "ABC123", "totalPaid": 30.0, "journeys": [
        {"segments": [{"flightNumber": 1234, "departureDate": "2026-11-12T07:05:00", "origin": "DEN", "destination": "LAS"}]},
        {"segments": [{"flightNumber": 1235, "departureDate": "2026-11-15T18:10:00", "origin": "LAS", "destination": "DEN"}]},
    ]},
    {"confirmationNumber": "XYZ789", "flights": [
        {"flightNo": "88", "departureDateTime": "2026-09-02 09:30", "departureStation": {"code": "MCO"}, "arrivalStation": {"code": "DEN"}}]},
]}


def test_parse_trips_from_account_json():
    trips = mytrips.parse_trips([MYTRIPS])
    assert [(t["date"], t["origin"], t["dest"], t["flight_no"]) for t in trips] == [
        ("2026-09-02", "MCO", "DEN", "88"), ("2026-11-12", "DEN", "LAS", "1234"), ("2026-11-15", "LAS", "DEN", "1235")]
    assert trips[1]["confirmation"] == "ABC123" and trips[1]["paid"] == 15.0  # booking total split over 2 flights
    assert trips[0]["confirmation"] == "XYZ789" and trips[0]["paid"] is None
    assert trips[0]["depart_time"] == "09:30"


def test_import_fills_fares_from_checks_and_skips_duplicates(tmp_path):
    conn = db.connect(str(tmp_path / "i.db"))
    db.save_check(conn, "DEN", "LAS", "2026-11-12", "ok", [{"flight_numbers": ["1234"], "standard_fare": 129.0, "gowild_price": 14.0}])
    trips = mytrips.parse_trips([MYTRIPS])
    assert db.import_trips(conn, trips) == 3
    assert db.import_trips(conn, trips) == 0
    row = conn.execute("SELECT * FROM trips WHERE flight_no='1234'").fetchone()
    assert row["cash_fare"] == 129.0 and row["paid"] == 15.0 and row["notes"] == "imported"
    assert "add cash fare" in conn.execute("SELECT notes FROM trips WHERE flight_no='88'").fetchone()["notes"]


@pytest.mark.skipif(browser.fcntl is None, reason="no file locks on Windows")
def test_profile_lock_blocks_a_second_run(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", str(tmp_path))
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,time,sys; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)"),
        str(tmp_path / "browser-profile.lock")], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    with pytest.raises(RuntimeError):
        with browser.ProfileLock("browser-profile", wait_min=0.02):
            pass
    holder.kill()
    holder.wait()
    with browser.ProfileLock("browser-profile", wait_min=0.05):
        pass


def test_leftover_singleton_files_are_cleared(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    os.symlink(f"host-{dead.pid}", prof / "SingletonLock")
    (prof / "SingletonCookie").write_text("x")
    browser.close_leftover_browser(str(prof))
    assert not os.path.lexists(prof / "SingletonLock") and not (prof / "SingletonCookie").exists()


def test_trip_fares_can_be_edited_and_import_button_shows(tmp_path):
    path = str(tmp_path / "e.db")
    client = create_app(path).test_client()
    client.post("/value", data={"date": "2026-11-12", "origin": "DEN", "dest": "LAS", "cash_fare": "0", "paid": "0"})
    client.post("/value", data={"action": "update", "id": "1", "cash_fare": "150", "paid": "12.5"})
    row = db.connect(path).execute("SELECT cash_fare, paid FROM trips").fetchone()
    assert (row["cash_fare"], row["paid"]) == (150.0, 12.5)
    page = client.get("/value").get_data(as_text=True)
    assert "Import from my Frontier account" in page
    assert "<title>Pass value, GoWild Finder</title>" in page
