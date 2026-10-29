"""Open the app's Chromium on one of its own profiles, one run per profile at a time.

Two runs on the same profile clash: Chromium hands the second launch to the
first window ("Opening in existing browser session") and the second run dies.
So each profile gets a lock file. A run waits for the lock, and once it holds
it, any Chromium still sitting on that profile is a leftover from a crashed
run, which it closes before launching.
"""
import os
import signal
import subprocess
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

try:
    import fcntl
except ImportError:  # Windows: no lock, runs must not overlap
    fcntl = None


def profile_path(name: str) -> str:
    return os.path.join(ROOT, name)


class ProfileLock:
    def __init__(self, name: str, wait_min: float = 90):
        self.path = profile_path(name) + ".lock"
        self.name, self.wait_min = name, wait_min

    def __enter__(self):
        if fcntl is None:
            return self
        self.fh = open(self.path, "w")
        deadline, told = time.time() + self.wait_min * 60, False
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError(f"Another run has been using {self.name} for over {self.wait_min} minutes")
                if not told:
                    print("Another check is using the browser; waiting for it to finish...", flush=True)
                    told = True
                time.sleep(min(5, max(0.1, deadline - time.time())))
        close_leftover_browser(profile_path(self.name))
        return self

    def __exit__(self, *exc):
        if fcntl is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def close_leftover_browser(profile: str):
    """Chromium records its pid in SingletonLock -> 'hostname-pid'. Close that process."""
    lock = os.path.join(profile, "SingletonLock")
    try:
        target = os.readlink(lock)
        pid = int(target.rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return
    if _is_chromium(pid):
        _terminate(pid)
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.remove(os.path.join(profile, f))
        except OSError:
            pass


def _is_chromium(pid: int) -> bool:
    """Only close the pid if it's still a Chromium (pids get reused)."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True).stdout
    except OSError:
        return False
    return "chrom" in out.lower()


def _terminate(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Closed a leftover browser window (pid {pid}).", flush=True)
        for _ in range(20):
            time.sleep(0.25)
            os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
