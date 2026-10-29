"""Paid seat prices for a trip you've booked.

Frontier only shows seat prices on the seat map (Manage Trip, or during
booking). The app opens a browser window with its own "account" profile; you
log in and open the seat map yourself, and the app reads the seat data the page
loads. It never clicks or buys anything. Close the window when done.
"""
import json
import os
import re
import time

from .frontier import DEBUG_DIR, _walk, _num

START_URL = "https://www.flyfrontier.com/"
_SEAT_ID = re.compile(r"^\d{1,2}[A-K]$")
_SEAT_KEY = re.compile(r"^(seat|seatNumber|seatDesignator|unitDesignator|designator|seatId|number)$", re.I)
_PRICE_KEY = re.compile(r"(price|amount|fee|cost)", re.I)
_AVAIL_KEY = re.compile(r"(avail|open|sellable|status|assignable)", re.I)
_TYPE_KEY = re.compile(r"(seatType|type|category|group|tier|productName|name|class)", re.I)

# Frontier seat products, best first; matched loosely against the page's labels.
TYPES = ["First Class", "UpFront Plus", "Premium", "Preferred", "Standard"]


def _seat_type(label: str | None) -> str:
    if label:
        low = label.lower()
        for t in TYPES:
            if t.lower().replace(" ", "") in low.replace(" ", ""):
                return t
        if "stretch" in low or "extra" in low or "legroom" in low:
            return "Premium"
    return label or "Unknown"


def _available(d: dict) -> bool:
    for k, v in d.items():
        if _AVAIL_KEY.search(k):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in ("open", "available", "true", "o", "sellable")
    return True


def parse_seats(blobs) -> list[dict]:
    """Seat-shaped dicts (a seat id like '12C' plus a price) from captured JSON."""
    seats = {}
    for blob in blobs:
        for d in _walk(blob):
            sid = next((str(v).upper() for k, v in d.items() if _SEAT_KEY.search(k) and isinstance(v, (str, int)) and _SEAT_ID.match(str(v).upper())), None)
            if not sid:
                continue
            price = None
            for x in _walk(d):
                for k, v in x.items():
                    if _PRICE_KEY.search(k) and isinstance(v, (int, float, str)) and _num(v) is not None:
                        price = _num(v)
                        break
                if price is not None:
                    break
            label = next((v for x in _walk(d) for k, v in x.items() if _TYPE_KEY.search(k) and isinstance(v, str) and not _SEAT_ID.match(v.upper())), None)
            seat = {"seat": sid, "row": int(sid[:-1]), "type": _seat_type(label), "price": price, "available": _available(d)}
            # keep the richest record per seat
            if sid not in seats or (seats[sid]["price"] is None and price is not None):
                seats[sid] = seat
    return sorted(seats.values(), key=lambda s: (s["row"], s["seat"]))


def summarize(seats: list[dict]) -> list[dict]:
    """Per seat type: how many are open and the price range."""
    out = {}
    for s in seats:
        if not s["available"]:
            continue
        g = out.setdefault(s["type"], {"type": s["type"], "open": 0, "min": None, "max": None, "seats": []})
        g["open"] += 1
        g["seats"].append(s["seat"])
        if s["price"] is not None:
            g["min"] = s["price"] if g["min"] is None else min(g["min"], s["price"])
            g["max"] = s["price"] if g["max"] is None else max(g["max"], s["price"])
    order = {t: i for i, t in enumerate(TYPES)}
    return sorted(out.values(), key=lambda g: order.get(g["type"], len(TYPES)))


def capture(trip_label: str, timeout_min: float = 15) -> tuple[list[dict], list]:
    """Open a browser, let the user reach the seat map, return (seats, raw blobs)."""
    from playwright.sync_api import sync_playwright

    blobs = []

    def on_response(resp):
        if "json" in (resp.headers.get("content-type") or ""):
            try:
                body = resp.text()
                if re.search(r"seat", body, re.I) and re.search(r'"\d{1,2}[A-K]"', body):
                    blobs.append(json.loads(body))
            except Exception:
                pass

    profile = os.path.join(os.path.dirname(__file__), "..", "account-profile")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile, headless=False, viewport={"width": 1280, "height": 900})
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL)
        print(f"Seat prices for {trip_label}:")
        print("  1. Log in and open My Trips > your trip > seats (the seat map).")
        print("  2. Once the seat map shows, close the browser window.")
        deadline = time.time() + timeout_min * 60
        closed = []
        ctx.on("close", lambda *_: closed.append(True))
        while not closed and time.time() < deadline:
            try:
                page = ctx.pages[0] if ctx.pages else None
                if page is None:
                    break
                page.wait_for_timeout(1000)
            except Exception:
                break
        try:
            ctx.close()
        except Exception:
            pass

    seats = parse_seats(blobs)
    if blobs and not seats:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"seats-{int(time.time())}.json"), "w") as fh:
            json.dump(blobs, fh)
    return seats, blobs
