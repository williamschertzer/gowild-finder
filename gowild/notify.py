"""Push alerts to your iPhone when new GoWild dates show up.

Uses ntfy (ntfy.sh): free, no account. Install the ntfy app on the iPhone and
subscribe to the private topic shown on the app's iPhone tab. Alerts pass
through ntfy's public server, so they only say the route, date and count.
"""
import secrets
import urllib.request

from . import db

SERVER = "https://ntfy.sh"


def topic(conn) -> str:
    t = db.get_setting(conn, "ntfy_topic")
    if not t:
        t = "gowild-" + secrets.token_hex(8)
        db.set_setting(conn, "ntfy_topic", t)
    return t


def enabled(conn) -> bool:
    return db.get_setting(conn, "alerts", "off") == "on"


def send(conn, title: str, body: str, click: str | None = None, force: bool = False) -> bool:
    if not (force or enabled(conn)):
        return False
    req = urllib.request.Request(f"{SERVER}/{topic(conn)}", data=body.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", "airplane")
    if click:
        req.add_header("Click", click)
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except OSError as e:
        print(f"Couldn't send alert: {e}")
        return False


def new_finds_message(finds) -> tuple[str, str]:
    """finds: list of (origin, dest, date, n_flights, early)."""
    early = sum(1 for f in finds if f[4])
    title = f"{len(finds)} new GoWild date{'s' if len(finds) != 1 else ''}" + (f" ({early} Early Booking)" if early else "")
    lines = [f"{o}→{d} {day}: {n} flight{'s' if n != 1 else ''}{' · early' if e else ''}" for o, d, day, n, e in finds[:12]]
    if len(finds) > 12:
        lines.append(f"…and {len(finds) - 12} more")
    return title, "\n".join(lines)
