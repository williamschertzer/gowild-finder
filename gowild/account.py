"""A browser window you drive yourself, logged in to your Frontier account.

Used for seat prices and importing trips. The app opens flyfrontier.com in its
own profile (account-profile/, which keeps you logged in between runs), you
click through to the page it needs, and it reads the JSON that page loads.
It never clicks, books or buys anything. Close the window when done.
"""
import json
import os
import re
import time

from .browser import ProfileLock, profile_path
from .frontier import DEBUG_DIR

START_URL = "https://www.flyfrontier.com/"
PROFILE = "account-profile"


def capture(keep, instructions: list[str], timeout_min: float = 15) -> list:
    """Collect JSON responses for which keep(body_text) is true until the window closes."""
    from playwright.sync_api import sync_playwright

    blobs = []

    def on_response(resp):
        if "json" in (resp.headers.get("content-type") or ""):
            try:
                body = resp.text()
                if keep(body):
                    blobs.append(json.loads(body))
            except Exception:
                pass

    with ProfileLock(PROFILE, wait_min=1), sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile_path(PROFILE), headless=False, viewport={"width": 1280, "height": 900})
        ctx.on("response", on_response)
        closed = []
        ctx.on("close", lambda *_: closed.append(True))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL)
        for i, line in enumerate(instructions, 1):
            print(f"  {i}. {line}")
        deadline = time.time() + timeout_min * 60
        while not closed and time.time() < deadline:
            try:
                if not ctx.pages:
                    break
                ctx.pages[0].wait_for_timeout(1000)
            except Exception:
                break
        try:
            ctx.close()
        except Exception:
            pass
    return blobs


def save_debug(name: str, blobs) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{name}-{int(time.time())}.json")
    with open(path, "w") as fh:
        json.dump(blobs, fh)
    return path


def looks_like(pattern: str):
    rx = re.compile(pattern, re.I | re.S)
    return lambda body: bool(rx.search(body))
