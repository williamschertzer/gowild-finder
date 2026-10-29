"""Look up GoWild fares on flyfrontier.com.

Frontier has no public API, so this drives a real browser through the same
search page you'd use, logged out, one date at a time, with a pause between
searches. It never touches your Frontier account.

The results page embeds its flight list as JSON. Frontier changes field names
from time to time, so the parser looks for anything shaped like a flight with a
GoWild fare rather than relying on exact names. When it can't make sense of a
page, it saves the HTML to debug/ so the parser can be fixed.
"""
import html as htmllib
import json
import os
import random
import re
import time
from datetime import date
from urllib.parse import quote

from .browser import ProfileLock, profile_path

SEARCH_URL = "https://booking.flyfrontier.com/Flight/InternalSelect?o1={o}&d1={d}&dd1={dd}&ADT=1&mon=true&promo="
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "debug")


class Blocked(Exception):
    """Frontier's bot protection showed a challenge instead of results."""


def search_url(origin: str, dest: str, d: str) -> str:
    dd = date.fromisoformat(d).strftime("%b %d, %Y")
    return SEARCH_URL.format(o=origin, d=dest, dd=quote(dd))


# ---------------------------------------------------------------- parsing

_GW = re.compile(r"go_?wild", re.I)
_DEP = re.compile(r"depart", re.I)
_ARR = re.compile(r"arriv", re.I)
_DT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def extract_json_blobs(page_html: str) -> list:
    """Pull JSON objects out of the results page's scripts."""
    blobs = []
    for m in re.finditer(r"FlightData\s*=\s*(['\"])(.*?)\1\s*;", page_html, re.S):
        blobs.append(m.group(2))
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', page_html, re.S):
        blobs.append(m.group(1))
    out = []
    for raw in blobs:
        for candidate in (raw, htmllib.unescape(raw)):
            try:
                out.append(json.loads(candidate))
                break
            except ValueError:
                continue
    return out


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _has_key(obj, pattern) -> bool:
    return any(isinstance(d, dict) and any(pattern.search(k) for k in d) for d in _walk(obj))


def _first(obj, pattern, want=(int, float, str, bool)):
    for d in _walk(obj):
        for k, v in d.items():
            if pattern.search(k) and isinstance(v, want) and v not in ("", None):
                return v
    return None


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _is_flight(d: dict) -> bool:
    return any(_GW.search(k) for k in d) and _has_key(d, _DEP)


def _times(d):
    deps = [v for x in _walk(d) for k, v in x.items() if _DEP.search(k) and isinstance(v, str) and _DT.search(v)]
    arrs = [v for x in _walk(d) for k, v in x.items() if _ARR.search(k) and isinstance(v, str) and _DT.search(v)]
    return (min(deps) if deps else None), (max(arrs) if arrs else None)


def parse_flight(d: dict) -> dict:
    gw_flag = _first(d, re.compile(r"go_?wild.*(enabled|available)", re.I), (bool,))
    gw_price = _num(_first(d, re.compile(r"go_?wild.*(fare|price|amount)$", re.I), (int, float, str)))
    gw_seats = _first(d, re.compile(r"go_?wild.*seat", re.I), (int, float))
    std = _num(_first(d, re.compile(r"^(standardFare|standard_fare|fare|basicFare|price)$", re.I), (int, float, str)))
    disc = _num(_first(d, re.compile(r"discountDenFare|denFare|discount.*fare", re.I), (int, float, str)))
    nums = sorted({str(x["flightNumber"]) for x in _walk(d) if x.get("flightNumber") is not None})
    stops = _first(d, re.compile(r"^stops(Text)?$", re.I), (int, str))
    dep, arr = _times(d)
    available = bool(gw_flag) if gw_flag is not None else (gw_price is not None and gw_price > 0)
    if gw_seats is not None and gw_seats <= 0:
        available = False
    return {
        "depart": dep,
        "arrive": arr,
        "flight_numbers": nums,
        "stops": stops,
        "gowild": available,
        "gowild_price": gw_price,
        "gowild_seats": int(gw_seats) if gw_seats is not None else None,
        "standard_fare": std,
        "discount_den_fare": disc,
    }


def find_flights(blobs) -> list[dict]:
    """Innermost dicts that look like a flight carrying GoWild info."""
    found = []
    for blob in blobs:
        candidates = [d for d in _walk(blob) if _is_flight(d)]
        ids = {id(d) for d in candidates}
        for d in candidates:
            inner = any(id(x) in ids for x in _walk(d) if x is not d)
            if not inner:
                found.append(parse_flight(d))
    uniq = {(f["depart"], tuple(f["flight_numbers"])): f for f in found}
    return sorted(uniq.values(), key=lambda f: f["depart"] or "")


def looks_blocked(page_html: str, title: str = "") -> bool:
    markers = ("px-captcha", "Press & Hold", "Access Denied", "captcha-delivery", "Request unsuccessful")
    return any(m.lower() in (page_html + title).lower() for m in markers)


def no_flights_page(page_html: str) -> bool:
    return bool(re.search(r"no flights|not available|no results", page_html, re.I))


def parse_results(page_html: str, extra_blobs=()) -> tuple[str, list[dict]]:
    """Return (status, gowild_flights). status: ok | no_flights | unparsed."""
    flights = find_flights(list(extract_json_blobs(page_html)) + list(extra_blobs))
    if flights:
        return "ok", [f for f in flights if f["gowild"]]
    if no_flights_page(page_html):
        return "no_flights", []
    return "unparsed", []


# ---------------------------------------------------------------- browsing

class FrontierBrowser:
    """A logged-out Chromium with its own profile, separate from your normal browser."""

    def __init__(self, headless: bool = False, min_delay: float = 15, max_delay: float = 40, profile: str = "browser-profile"):
        self.headless, self.min_delay, self.max_delay = headless, min_delay, max_delay
        self.profile = profile
        self._last = 0.0

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._lock = ProfileLock(self.profile).__enter__()
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            profile_path(self.profile), headless=self.headless, viewport={"width": 1280, "height": 900}, locale="en-US"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
            self._pw.stop()
        finally:
            self._lock.__exit__(*exc)

    def _pace(self):
        wait = self._last + random.uniform(self.min_delay, self.max_delay) - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def lookup(self, origin: str, dest: str, d: str) -> tuple[str, list[dict]]:
        self._pace()
        captured = []

        def on_response(resp):
            if "json" in (resp.headers.get("content-type") or ""):
                try:
                    body = resp.text()
                    if _GW.search(body):
                        captured.append(json.loads(body))
                except Exception:
                    pass

        self.page.on("response", on_response)
        try:
            self.page.goto(search_url(origin, dest, d), wait_until="domcontentloaded", timeout=60000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            page_html, title = self.page.content(), self.page.title()
        finally:
            self.page.remove_listener("response", on_response)

        if looks_blocked(page_html, title):
            raise Blocked(f"Frontier showed a bot check on {origin}-{dest} {d}")
        status, flights = parse_results(page_html, captured)
        if status == "unparsed":
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(os.path.join(DEBUG_DIR, f"{origin}-{dest}-{d}.html"), "w") as fh:
                fh.write(page_html)
        return status, flights
