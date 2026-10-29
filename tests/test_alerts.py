from gowild import db, notify
from gowild.web import create_app


def test_save_check_reports_new_finds_and_keeps_data_on_failure(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    f = [{"flight_numbers": ["1"]}]
    assert db.save_check(conn, "DEN", "LAS", "2026-11-20", "no_flights", []) is False
    assert db.save_check(conn, "DEN", "LAS", "2026-11-20", "ok", f) is True
    assert db.save_check(conn, "DEN", "LAS", "2026-11-20", "ok", f) is False  # already known
    assert db.save_check(conn, "DEN", "LAS", "2026-11-20", "blocked", []) is False
    row = conn.execute("SELECT status, flights FROM checks").fetchone()
    assert row["status"] == "ok" and row["flights"] != "[]"


def test_alert_message():
    title, body = notify.new_finds_message([("DEN", "LAS", "2026-11-20", 2, True), ("DEN", "MCO", "2026-11-21", 1, False)])
    assert title == "2 new GoWild dates (1 Early Booking)"
    assert body.splitlines() == ["DEN→LAS 2026-11-20: 2 flights · early", "DEN→MCO 2026-11-21: 1 flight"]


def test_alerts_off_by_default_and_toggle(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    sent = []
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: sent.append(req) or type("R", (), {"close": lambda s: None})())
    conn = db.connect(path)
    assert notify.send(conn, "t", "b") is False and not sent
    client = create_app(path).test_client()
    client.post("/alerts", data={"action": "on"})
    assert notify.enabled(conn)
    r = client.post("/alerts", data={"action": "test"})
    assert "sent=ok" in r.headers["Location"] and sent[-1].full_url == f"https://ntfy.sh/{notify.topic(conn)}"
    assert "Turn alerts off" in client.get("/phone").get_data(as_text=True)
    assert client.post("/alerts", data={"action": "on"}, environ_base={"REMOTE_ADDR": "192.168.1.9"}).status_code == 403
