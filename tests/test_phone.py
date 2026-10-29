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
