"""Import past and upcoming trips from your Frontier account.

You open each trip's details in the account window; the app reads the flights
from those pages and adds any not already in your trip log. A trip's details
page (booking.flyfrontier.com/Booking/...) is built on Frontier's server, so the
flights are in its HTML as journey keys like
  F9~4277~ ~~IAD~11/28/2026 13:09~LAS~11/28/2026 15:31~~
(carrier, flight, from, local departure, to, local arrival). JSON the pages
load is read too, in case Frontier moves trips to an API.
Frontier doesn't say what a flight would have cost in cash, so the cash fare
comes from what this app saw when it found that flight, or you fill it in.
"""
import re

from . import account

_CODE = re.compile(r"^[A-Z]{3}$")
_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})")
_FLIGHT_KEY = re.compile(r"^(flightNumber|flightNo|flight_number|number)$", re.I)
_DEP_KEY = re.compile(r"depart.*(date|time)|^std$|scheduledDeparture", re.I)
_ORIG_KEY = re.compile(r"^(origin|departureStation|departureAirport|departureCode|from|originCode|departureStationCode)$", re.I)
_DEST_KEY = re.compile(r"^(destination|arrivalStation|arrivalAirport|arrivalCode|to|destinationCode|arrivalStationCode)$", re.I)
_PNR_KEY = re.compile(r"recordLocator|confirmation|pnr|bookingCode|locator", re.I)
_TOTAL_KEY = re.compile(r"^(totalPaid|amountPaid|totalAmount|total|totalCost|balancePaid)$", re.I)


def _scalar(d, rx, ok):
    for k, v in d.items():
        if rx.search(k) and isinstance(v, (str, int, float)) and ok(v):
            return v
    return None


def _code(d, rx):
    v = _scalar(d, rx, lambda v: isinstance(v, str) and _CODE.match(v.upper()))
    if v is None:  # e.g. {"origin": {"code": "DEN"}}
        for k, sub in d.items():
            if rx.search(k) and isinstance(sub, dict):
                v = _scalar(sub, re.compile(r"code|iata|station", re.I), lambda v: isinstance(v, str) and _CODE.match(v.upper()))
                if v:
                    break
    return v.upper() if v else None


def _segment(d):
    num = _scalar(d, _FLIGHT_KEY, lambda v: re.fullmatch(r"\d{1,4}", str(v)) is not None)
    dep = _scalar(d, _DEP_KEY, lambda v: isinstance(v, str) and _DT.match(v))
    o, dst = _code(d, _ORIG_KEY), _code(d, _DEST_KEY)
    if num is None or not dep or not o or not dst or o == dst:
        return None
    m = _DT.match(dep)
    return {"date": m.group(1), "depart_time": m.group(2), "origin": o, "dest": dst, "flight_no": str(num)}


# carrier~flight~~~FROM~MM/DD/YYYY HH:MM~TO~MM/DD/YYYY HH:MM
_SELL_KEY = re.compile(
    r"F9~\s*(\d{1,4})\s*~[^~<>\"]*~[^~<>\"]*~([A-Z]{3})~(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2})"
    r"~([A-Z]{3})~(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2})")
_PAGE_PNR = [
    re.compile(r"addCustomAttribute\(\s*'PNR'\s*,\s*'([A-Z0-9]{6})'"),
    re.compile(r"\"bookingcode\"\s*:\s*\"([A-Z0-9]{6})\""),
    re.compile(r"name=\"rl\"\s+value=\"([A-Z0-9]{6})\""),
    re.compile(r"[?&]rl=([A-Z0-9]{6})\b"),
]


def parse_page(url: str, page_html: str) -> list[dict]:
    """Flights on one trip-details page. Pages without a booking code are skipped, so
    flight search results opened in the same window never land in the trip log."""
    pnr = next((m.group(1) for rx in _PAGE_PNR for m in [rx.search(url + "\n" + page_html)] if m), None)
    if not pnr:
        return []
    flights = {}
    for m in _SELL_KEY.finditer(page_html):
        num, o, mo, dd, yy, hm, dst, amo, add, ayy, ahm = m.groups()
        if o == dst:
            continue
        date = f"{yy}-{mo}-{dd}"
        flights[(date, o, dst, num)] = {
            "date": date, "depart_time": hm, "origin": o, "dest": dst, "flight_no": num,
            "arrive": f"{ayy}-{amo}-{add}T{ahm}", "confirmation": pnr,
        }
    out = sorted(flights.values(), key=lambda t: (t["date"], t["depart_time"]))
    fare = _fare_paid(page_html)
    for t in out:  # per traveler, split across the booking's flights
        t["paid"] = round(fare / len(out), 2) if fare is not None else None
    return out


def _fare_paid(page_html: str) -> float | None:
    """What the flights cost with GoWild, per traveler: fare plus taxes and fees.

    Bags, seats and bundles are left out on purpose: you'd buy those with or without
    the pass, so counting them would understate what GoWild saved. The page's
    `var attrs = {...,"BaseFare":112.55,...,"TaxRevenue":119.64,...}` has the split;
    `var totalPrice` (everything, extras included) is the fallback.
    """
    pax = re.search(r"var\s+totalPassengers\s*=\s*(\d+)", page_html)
    pax = max(1, int(pax.group(1))) if pax else 1
    base = re.search(r'"BaseFare"\s*:\s*([\d.]+)', page_html)
    tax = re.search(r'"TaxRevenue"\s*:\s*([\d.]+)', page_html)
    if base and tax:
        return (float(base.group(1)) + float(tax.group(1))) / pax
    total = re.search(r"var\s+totalPrice\s*=\s*([\d.]+)", page_html)
    return float(total.group(1)) / pax if total else None


def parse_pages(pages) -> list[dict]:
    found = {}
    for url, page_html in pages:
        for t in parse_page(url, page_html):
            found[(t["date"], t["origin"], t["dest"], t["flight_no"])] = t
    return sorted(found.values(), key=lambda t: (t["date"], t["depart_time"]))


def parse_trips(blobs) -> list[dict]:
    trips = {}

    def walk(obj, pnr, total):
        if isinstance(obj, list):
            for x in obj:
                walk(x, pnr, total)
            return
        if not isinstance(obj, dict):
            return
        pnr = _scalar(obj, _PNR_KEY, lambda v: isinstance(v, str) and re.fullmatch(r"[A-Z0-9]{6}", v)) or pnr
        t = _scalar(obj, _TOTAL_KEY, lambda v: isinstance(v, (int, float)) or re.fullmatch(r"\$?[\d,]+(\.\d+)?", str(v)))
        if t is not None:
            total = (float(str(t).replace("$", "").replace(",", "")), id(obj))
        seg = _segment(obj)
        if seg:
            key = (seg["date"], seg["origin"], seg["dest"], seg["flight_no"])
            trips[key] = {**seg, "confirmation": pnr, "_total": total}
            return
        for v in obj.values():
            walk(v, pnr, total)

    for b in blobs:
        walk(b, None, None)

    # split a booking's total across its flights
    per_booking = {}
    for t in trips.values():
        if t["_total"]:
            per_booking.setdefault(t["_total"][1], []).append(t)
    out = []
    for t in trips.values():
        tot = t.pop("_total")
        t["paid"] = round(tot[0] / len(per_booking[tot[1]]), 2) if tot else None
        out.append(t)
    return sorted(out, key=lambda t: (t["date"], t["depart_time"]))


def capture() -> tuple[list[dict], bool]:
    """Returns (trips, whether any Frontier pages were seen at all)."""
    print("Import trips from your Frontier account:")
    pages = []
    blobs = account.capture(
        account.looks_like(r"(flightNumber|flightNo).*(depart|std)|(depart|std).*(flightNumber|flightNo)"),
        ["Log in, go to My Trips, and open each trip you want to import (its flight details page).",
         "Wait for each trip's flights to show, then close the browser window."],
        pages=pages,
    )
    found = {}
    for t in parse_trips(blobs) + parse_pages(pages):  # a trip page's details win over JSON's
        key = (t["date"], t["origin"], t["dest"], t["flight_no"])
        found[key] = {**found.get(key, {}), **{k: v for k, v in t.items() if v is not None}}
    trips = sorted(found.values(), key=lambda t: (t["date"], t["depart_time"]))
    if not trips and (blobs or pages):
        account.save_debug("trips", {"json": blobs, "pages": pages})
    return trips, bool(blobs or pages)
