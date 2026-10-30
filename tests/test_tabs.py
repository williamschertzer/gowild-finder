import http.server
import threading

import pytest

from gowild import __main__ as cli, browser, db, frontier

PAGES = {
    "DEN": "<html><body>No flights available</body></html>",
    "LAS": "<html><title>Access Denied</title><body>px-captcha</body></html>",
    "MCO": "<html><body>No flights available</body></html>",
}


class FakeBrowser:
    calls = []

    def __init__(self, tabs=1, **kw):
        self.tabs = tabs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def lookup_many(self, tasks):
        FakeBrowser.calls.append(list(tasks))
        out = []
        for t in tasks:
            if t[1] == "LAS":
                out.append((t, "blocked", [], frontier.Blocked("bot check")))
            else:
                out.append((t, "no_flights", [], None))
        return out


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FrontierBrowser", FakeBrowser)
    FakeBrowser.calls = []
    return db.connect(str(tmp_path / "t.db"))


def test_runs_in_rounds_of_the_tabs_setting(conn):
    db.set_setting(conn, "tabs", 3)
    tasks = [("DEN", "MCO", f"2026-11-{d:02d}") for d in range(10, 17)]
    assert cli.run_lookups(conn, tasks, True, 40) == 0
    assert [len(b) for b in FakeBrowser.calls] == [3, 3, 1]
    assert len(db.get_checks(conn, ["DEN"], ["MCO"], "2026-11-01", "2026-11-30")) == 7


def test_a_bot_check_in_one_tab_saves_the_others_then_stops(conn, capsys):
    tasks = [("DEN", "MCO", "2026-11-10"), ("DEN", "LAS", "2026-11-10"), ("DEN", "MCO", "2026-11-11")]
    assert cli.run_lookups(conn, tasks, True, 40, tabs=2) == 2
    assert len(FakeBrowser.calls) == 1  # the second round never ran
    assert [c["status"] for c in db.get_checks(conn, ["DEN"], ["MCO"], "2026-11-01", "2026-11-30")] == ["no_flights"]
    assert "back to 1" in capsys.readouterr().out


def test_tabs_setting_is_clamped(conn):
    db.set_setting(conn, "tabs", 9)
    cli.run_lookups(conn, [("DEN", "MCO", f"2026-11-{d:02d}") for d in range(10, 16)], True, 40)
    assert [len(b) for b in FakeBrowser.calls] == [3, 3]


def test_real_browser_loads_tabs_side_by_side(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            dest = self.path.split("d1=")[1][:3]
            body = PAGES[dest].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(frontier, "SEARCH_URL", f"http://127.0.0.1:{srv.server_port}/?o1={{o}}&d1={{d}}&dd1={{dd}}")
    monkeypatch.setattr(browser, "ROOT", str(tmp_path))
    try:
        with frontier.FrontierBrowser(headless=True, min_delay=0, max_delay=0, tabs=3) as br:
            assert len(br.pages) == 3
            got = br.lookup_many([("ORD", "DEN", "2026-11-10"), ("ORD", "LAS", "2026-11-10"), ("ORD", "MCO", "2026-11-10")])
    except Exception as e:  # no Chromium on this machine
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    finally:
        srv.shutdown()
    assert [(t[1], s) for t, s, _, _ in got] == [("DEN", "no_flights"), ("LAS", "blocked"), ("MCO", "no_flights")]
    assert isinstance(got[1][3], frontier.Blocked)


def test_saved_searches_page_sets_searches_at_once(tmp_path):
    from gowild.web import create_app

    app = create_app(str(tmp_path / "w.db"))
    client = app.test_client()
    client.post("/searches", data={"name": "Ski", "origins": "DEN", "destinations": "SLC",
                                   "start_date": "2026-12-01", "end_date": "2026-12-10"})
    page = client.get("/").get_data(as_text=True)
    assert "Check all now" in page and '<option value="2" selected>' in page
    client.post("/settings/tabs", data={"tabs": "7"})
    assert db.get_setting(db.connect(str(tmp_path / "w.db")), "tabs") == "3"
    assert "about 7 minutes" in client.get("/").get_data(as_text=True)
