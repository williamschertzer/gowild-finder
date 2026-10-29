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
