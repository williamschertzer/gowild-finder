# GoWild Finder

A personal app for getting the most out of a Frontier GoWild pass. It runs on
your own computer.

- **Saved searches with a GoWild calendar.** Save routes and a date range. The
  app checks Frontier's search for each date and marks the days with GoWild
  fares, including the new **Early Booking** select flights you can book ahead.
- **Fly tomorrow.** One button checks every saved route for tomorrow (when
  regular GoWild seats open) and lists the flights by time with a Book link.
- **Pass value.** Log trips with the cash fare you avoided and see savings,
  what's left to break even, and how many more trips that takes.
- **Upgrade timing.** Set your Frontier Miles tier and each upcoming trip shows
  when check-in opens and when your UpFront Plus upgrade window starts.
- **Paid seat prices.** For a trip you've booked, see which seats are open by
  type (UpFront Plus, Premium, Preferred, Standard) and what each costs.

## Setup (once)

Needs Python 3.10 or newer.

```bash
git clone https://github.com/williamschertzer/gowild-finder
cd gowild-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Use

```bash
python -m gowild serve        # open http://127.0.0.1:5050
```

Add a search on the Saved searches page, then press **Check now**. A browser
window opens and works through the dates; leave it alone until it finishes. You
can also run checks from the terminal:

```bash
python -m gowild plan         # list the dates a check would look up
python -m gowild check        # run up to 40 due lookups (--max to change)
python -m gowild tomorrow     # check every saved route for tomorrow
```

### Seat prices for a booked trip

Log the trip on **Pass value & trips**, then press **Seat prices** on its row
(or run `python -m gowild seats --trip ID`). A browser window opens. Log in to
Frontier, go to My Trips, open that trip's seat map, then close the window. The
app reads the seat prices the page loaded and shows them under the trip. It
never selects or buys a seat. This window keeps its own login in
`account-profile/`, separate from the logged-out profile used for searches.

To keep the calendar fresh, schedule `python -m gowild check` a couple of times
a day (cron on macOS/Linux, Task Scheduler on Windows).

## How it works, and staying on Frontier's good side

Frontier has no API for GoWild. The app opens Frontier's normal search page in
a separate, **logged-out** browser profile and reads the flight list the page
already contains. Searches never use your account, and the app never books
anything or touches payment. (Seat prices are the one place you log in, by hand,
in a window you drive yourself.) You book yourself by clicking **Book**, which opens the same search in
your regular browser.

To stay low-impact it pauses 15 to 40 seconds between searches, caps each run
at 40 lookups, and skips dates checked in the last 12 hours, blackout dates,
and dates after the pass ends. If Frontier shows a bot check, the run stops.
Automated searching is a gray area under Frontier's site terms, so keep the
volume modest.

If a results page can't be read (Frontier changed its page), the HTML is saved
in `debug/` and the date shows red on the calendar. Share that file to get the
parser updated.

## Pass rules the app uses

From flyfrontier.com (checked Oct 2026), in `gowild/rules.py`:

- Domestic GoWild opens the day before departure; international 10 days before.
- Early Booking: select flights bookable further out, with a charge starting at
  $1 per passenger per direction, already included in the search price.
- Fall/Winter 2026 pass valid through Feb 28, 2027, with the posted blackout dates.

## Tests

```bash
pip install pytest && python -m pytest -q
```
