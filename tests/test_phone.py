from gowild import db, phone
from gowild.web import create_app


def test_mac_is_trusted_and_other_devices_need_the_key(tmp_path):
    path = str(tmp_path / "p.db")
    app = create_app(path, phone_access=True)
    mac = app.test_client()
    assert mac.get("/").status_code == 200
    assert "iPhone" in mac.get("/phone").get_data(as_text=True)

    iphone = app.test_client()
    lan = {"REMOTE_ADDR": "192.168.1.23"}
    r = iphone.get("/tomorrow", environ_base=lan)
    assert r.status_code == 403 and "Pair this phone" in r.get_data(as_text=True)
    assert iphone.get("/phone", environ_base=lan).status_code == 403
    assert iphone.get("/pair?key=wrong", environ_base=lan).status_code == 403

    k = phone.key(db.connect(path))
    r = iphone.get(f"/pair?key={k}", environ_base=lan)
    assert r.status_code == 302 and r.headers["Location"].endswith("/tomorrow")
    assert iphone.get("/tomorrow", environ_base=lan).status_code == 200
    assert iphone.get("/static/manifest.json", environ_base=lan).status_code == 200

    # making a new code signs the phone out
    mac.post("/phone")
    assert iphone.get("/tomorrow", environ_base=lan).status_code == 403


def test_pair_url_and_qr():
    url = phone.pair_url("192.168.1.5", "abc")
    assert url == "http://192.168.1.5:5050/pair?key=abc"
    assert phone.qr_svg(url).lstrip().startswith("<")


def test_job_status_endpoint_and_no_full_page_reload(tmp_path):
    client = create_app(str(tmp_path / "j.db")).test_client()
    j = client.get("/job/check").get_json()
    assert j["kind"] == "check" and j["running"] is False
    assert client.get("/job/nope").status_code == 404
    assert "setTimeout(() => location.reload()" not in client.get("/").get_data(as_text=True)


def test_stop_and_delete_end_a_running_check(tmp_path, monkeypatch):
    import subprocess
    import sys
    from gowild import web

    client = create_app(str(tmp_path / "s.db")).test_client()
    monkeypatch.setattr(web, "ROOT", str(tmp_path))
    web._jobs["check"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert client.get("/job/check").get_json()["running"]
    client.post("/searches", data={"name": "X", "origins": "DEN", "destinations": "LAS", "start_date": "2026-11-01", "end_date": "2026-11-05"})
    client.post("/searches/1/delete")
    assert not client.get("/job/check").get_json()["running"]
    assert "Stopped." in client.get("/job/check").get_json()["log"]

    web._jobs["tomorrow"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    client.post("/stop/tomorrow")
    assert web._jobs["tomorrow"].poll() is not None


def test_tailscale_qr_shows_when_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(phone, "tailscale_address", lambda: "100.64.1.2")
    page = create_app(str(tmp_path / "t.db")).test_client().get("/phone").get_data(as_text=True)
    assert "http://100.64.1.2:5050/pair?key=" in page and "Tailscale is connected" in page
    monkeypatch.setattr(phone, "tailscale_address", lambda: None)
    assert "Install <b>Tailscale</b>" in create_app(str(tmp_path / "t.db")).test_client().get("/phone").get_data(as_text=True)


def test_a_click_right_after_stop_does_not_start_another_check(tmp_path, monkeypatch):
    import subprocess
    import sys
    from gowild import web

    monkeypatch.setattr(web, "ROOT", str(tmp_path))
    started = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(web.subprocess, "Popen", lambda args, **kw: started.append(args) or real_popen([sys.executable, "-c", "pass"]))
    web._jobs.pop("check", None)
    web._stopped_at.pop("check", None)
    client = web.create_app(str(tmp_path / "s.db")).test_client()
    r = client.post("/stop/check", headers={"Referer": "http://localhost/"})
    assert r.headers["Location"].endswith("/#job")  # stays on the run's card
    client.post("/run/check", data={"search": "Thanksgiving"})
    assert started == []  # the stray click is ignored
    monkeypatch.setattr(web, "STOP_GRACE", 0)
    client.post("/run/check", data={"search": "Thanksgiving"})
    assert len(started) == 1  # a real click later still works


def test_automatic_checks_are_per_search_with_their_own_interval(tmp_path, monkeypatch):
    from gowild import __main__ as cli, planner

    path = str(tmp_path / "a.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    client = create_app(path).test_client()
    for name, dest in (("LA", "LAX"), ("Vegas", "LAS")):
        client.post("/searches", data={"name": name, "origins": "ATL", "destinations": dest, "start_date": "2026-12-01",
                                       "end_date": "2026-12-03", "weekdays": list("0123456")})
    searched = []
    monkeypatch.setattr(cli, "run_lookups", lambda conn, tasks, *a, **k: searched.append(tasks) or 0)
    assert cli.main(["auto"]) == 0 and searched == []  # new searches aren't automatic
    assert "searches only run when you press a Check button" in client.get("/").get_data(as_text=True)

    vegas = db.connect(path).execute("SELECT id FROM searches WHERE name='Vegas'").fetchone()["id"]
    client.post(f"/searches/{vegas}/auto", data={"auto": "on", "auto_hours": "6"})
    row = db.connect(path).execute("SELECT auto, auto_hours FROM searches WHERE id=?", (vegas,)).fetchone()
    assert (row["auto"], row["auto_hours"]) == (1, 6)
    cli.main(["auto"])
    assert searched and {t[1] for t in searched[-1]} == {"LAS"}  # only the search that's on

    # dates checked within that search's interval are left alone
    conn = db.connect(path)
    for t in searched[-1]:
        db.save_check(conn, *t, "ok", [])
    searched.clear()
    cli.main(["auto"])
    assert searched == []
    s = dict(conn.execute("SELECT * FROM searches WHERE id=?", (vegas,)).fetchone(), auto_hours=1)
    conn.execute("UPDATE checks SET checked_at='2000-01-01T00:00:00'")
    conn.commit()
    assert planner.auto_tasks(conn, [s], today=__import__("datetime").date(2026, 11, 1))

    client.post(f"/searches/{vegas}/auto", data={"auto_hours": "6"})  # unticked
    assert db.connect(path).execute("SELECT auto FROM searches WHERE id=?", (vegas,)).fetchone()["auto"] == 0
