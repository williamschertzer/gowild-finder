"""Use the app from your iPhone over home Wi-Fi.

The Mac keeps doing the checks (it has the browser); the iPhone just shows the
pages. Other devices on the network need a secret key, which the iPhone gets
once by scanning a QR code on the Mac and then keeps in a cookie.
"""
import io
import secrets
import socket
import subprocess

from . import db

PORT = 5050
COOKIE = "gowild_key"


def key(conn) -> str:
    k = db.get_setting(conn, "phone_key")
    if not k:
        k = secrets.token_urlsafe(16)
        db.set_setting(conn, "phone_key", k)
    return k


def _bonjour_name() -> str | None:
    """The Mac's real Bonjour name (System Settings > General > Sharing > Local hostname)."""
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3)
        name = out.stdout.strip()
        return f"{name}.local" if out.returncode == 0 and name else None
    except (OSError, subprocess.SubprocessError):
        return None


def lan_addresses() -> list[str]:
    """Addresses the iPhone can reach this Mac at: its Wi-Fi IP first, then its .local name.

    The IP is the most reliable; gethostname() on a Mac can be a router-assigned
    name (e.g. "macbook.lan") that the iPhone can't resolve, so the .local name
    comes from scutil instead.
    """
    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the LAN interface
            out.append(s.getsockname()[0])
    except OSError:
        pass
    name = _bonjour_name()
    if name:
        out.append(name)
    return [a for a in out if not a.startswith("127.")]


def pair_url(host: str, k: str) -> str:
    return f"http://{host}:{PORT}/pair?key={k}"


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
