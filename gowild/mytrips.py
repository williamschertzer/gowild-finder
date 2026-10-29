"""Import past and upcoming trips from your Frontier account.

You open My Trips (and past trips) in the account window; the app reads the
trip data those pages load and adds any flights not already in your trip log.
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


def capture() -> tuple[list[dict], list]:
    print("Import trips from your Frontier account:")
    blobs = account.capture(
        account.looks_like(r"(flightNumber|flightNo).*(depart|std)|(depart|std).*(flightNumber|flightNo)"),
        ["Log in and open My Trips. Open each upcoming trip you want, and Past Trips too.",
         "When you've opened them, close the browser window."],
    )
    trips = parse_trips(blobs)
    if blobs and not trips:
        account.save_debug("trips", blobs)
    return trips, blobs
