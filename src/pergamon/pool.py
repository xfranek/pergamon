"""DDoS-Guard sessions spread over mullid exit identities.

__ddg9_ carries the IP that solved the challenge, so a session only works through
the exit that minted it. Each exit `pergamon<i>` is a sticky mullid profile with its
own cookie file. Requests go to exits that hold a valid session and are not busy
minting; the others mint in the background. A refused session is re-minted on the
same exit; if minting fails there, the exit is rotated to a fresh relay.

    PERGAMON_MULLID=http://127.0.0.1:8888            mullid proxy (unset = direct, one exit)
    PERGAMON_MULLID_CONTROL=http://127.0.0.1:8889    mullid control API
    PERGAMON_POOL=4                                  number of exits

    pergamon-session            # mint every exit that needs it
    pergamon-session --force    # re-mint every exit
    pergamon-session --check    # only verify cached sessions
"""

import argparse
import itertools
import os
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests

from . import session

MULLID = os.environ.get("PERGAMON_MULLID", "")
CONTROL = os.environ.get("PERGAMON_MULLID_CONTROL", "http://127.0.0.1:8889")
SIZE = int(os.environ.get("PERGAMON_POOL", 4)) if MULLID else 1
MINT_ATTEMPTS = 2  # same exit once, then a rotated one
WARM_BACKOFF = 5 * 60  # after a failed background mint, leave the exit alone this long
# Anna's answers 429 above ~20 searches/min per IP (calibrated 2026-11-08: 18.9/min held
# for 150s, 22.5/min hit 429 in 20s). One in flight, 4s apart = 15/min, 75% of the limit.
MAX_INFLIGHT = int(os.environ.get("PERGAMON_INFLIGHT", 1))
GAP = float(os.environ.get("PERGAMON_GAP", 4.0))         # min seconds between request starts per exit
COOLDOWN = 60.0                                          # exit rests this long after a 429
QUEUE_TIMEOUT = float(os.environ.get("PERGAMON_QUEUE_TIMEOUT", 60))


class Busy(Exception):
    """No exit freed up within QUEUE_TIMEOUT."""


class Exit:
    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()  # one mint at a time per exit; other exits stay usable
        self.failed_at = 0.0
        self.inflight = 0      # guarded by _cv
        self.next_at = 0.0     # earliest start of the next request, guarded by _cv
        if MULLID:
            u = urlsplit(MULLID)
            url = f"{u.scheme}://{name}:x@{u.netloc}"  # mullid: profile = proxy username
            self.proxies: dict | None = {"http": url, "https": url}
            self.browser_proxy: dict | None = {"server": MULLID, "username": name, "password": "x"}
            self.state: Path = session.STATE.parent / "pool" / f"{name}.json"
        else:
            self.proxies = self.browser_proxy = None
            self.state = session.STATE

    @property
    def ready(self) -> bool:
        """Holds a valid session and is not minting right now."""
        return not self.lock.locked() and session.load(self.state) is not None

    def session(self, refused: float | None = None, force: bool = False) -> dict:
        """Cached session, minted if missing. `refused` = solved_at of a session the
        caller saw rejected: re-mint only if that same session is still on file, so
        concurrent refusals on one exit trigger a single mint."""
        with self.lock:
            s = None if force else session.load(self.state)
            if s is None or s["solved_at"] == refused:
                s = self._mint()
            return s

    def warm(self) -> None:
        """Mint in the background unless one is running or the last one failed recently."""
        if not self.lock.locked() and time.time() - self.failed_at > WARM_BACKOFF:
            threading.Thread(target=self._warm, daemon=True, name=f"mint-{self.name}").start()

    def _warm(self) -> None:
        try:
            self.session()
        except RuntimeError:
            self.failed_at = time.time()  # stays not-ready until the backoff passes
        finally:
            with _cv:
                _cv.notify_all()  # queued requests may now use this exit

    def _mint(self) -> dict:
        error: Exception | None = None
        for attempt in range(MINT_ATTEMPTS):
            if attempt and MULLID:
                self._rotate()
            try:
                s = session._solve(self.browser_proxy)
                if session.works(s, self.proxies):
                    session.save(s, self.state)
                    return s
                error = RuntimeError("minted session refused on its own exit")
            except Exception as e:  # challenge stuck, proxy/browser failure
                error = e
        raise RuntimeError(f"{self.name}: no usable session after {MINT_ATTEMPTS} mints") from error

    def _rotate(self) -> None:
        r = requests.post(f"{CONTROL}/profiles/{self.name}/rotate", timeout=15)
        if r.status_code != 404:  # 404: profile never used, so it has no relay to drop yet
            r.raise_for_status()

    def check(self) -> tuple[bool, int]:
        s = session.load(self.state)
        if s is None:
            return False, 0
        return session.works(s, self.proxies), int(s["expires_at"] - time.time())

    def cool(self, seconds: float = COOLDOWN) -> None:
        """Rate-limited (429): the session is fine, the IP just needs a rest."""
        with _cv:
            self.next_at = max(self.next_at, time.time() + seconds)


# mullid reads a "-xx" suffix as a country pin, so exit names carry no dash
EXITS = [Exit(f"pergamon{i}" if MULLID else "direct") for i in range(SIZE)]
_turn = itertools.cycle(range(SIZE))
_turn_lock = threading.Lock()
_cv = threading.Condition()  # guards Exit.inflight/next_at; signalled when a slot or session frees up


@contextmanager
def lease(exclude: Exit | None = None) -> Iterator[Exit]:
    """Hold a slot on a ready exit for one request, queueing until one is free.

    Respects MAX_INFLIGHT and GAP per exit; raises Busy after QUEUE_TIMEOUT."""
    deadline = time.time() + QUEUE_TIMEOUT
    with _cv:
        while True:
            now = time.time()
            candidates = [ex for ex in exits() if ex is not exclude and ex.ready and ex.inflight < MAX_INFLIGHT]
            if free := [ex for ex in candidates if ex.next_at <= now]:
                ex = free[0]
                ex.inflight += 1
                ex.next_at = now + GAP
                break
            if now >= deadline:
                raise Busy(f"all {SIZE} exits busy for {QUEUE_TIMEOUT:.0f}s")
            # wake when the soonest paced exit opens, a slot frees, or a mint lands
            soonest = min((ex.next_at for ex in candidates), default=now + 1)
            _cv.wait(min(deadline, soonest) - now)
    try:
        yield ex
    finally:
        with _cv:
            ex.inflight -= 1
            _cv.notify()


def exits() -> list[Exit]:
    """All exits, round-robin rotated, ready ones first. Not-ready exits are
    warmed in the background so they rejoin without a request waiting on them."""
    with _turn_lock:
        start = next(_turn)
    order = EXITS[start:] + EXITS[:start]
    ready = [ex for ex in order if ex.ready]
    for ex in order:
        if ex not in ready:
            ex.warm()
    return ready + [ex for ex in order if ex not in ready]


def egress() -> dict | None:
    """Proxies for traffic that needs no session (downloads): any exit will do."""
    return exits()[0].proxies


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-mint every exit")
    ap.add_argument("--check", action="store_true", help="only verify cached sessions")
    a = ap.parse_args()

    def run(ex: Exit) -> tuple[bool, str]:
        t0 = time.time()
        if a.check:
            ok, left = ex.check()
            return ok, f"{'ok' if ok else 'invalid'} ({left}s left)"
        try:
            ex.session(force=a.force)
        except RuntimeError as e:
            return False, f"FAILED {e} ({e.__cause__})"
        ok, left = ex.check()
        return ok, f"{ex.state}  ({left}s left, {time.time() - t0:.1f}s)"

    with ThreadPoolExecutor(max_workers=SIZE) as executor:  # each exit has its own lock
        results = list(executor.map(run, EXITS))
    for ex, (ok, line) in zip(EXITS, results):
        print(f"{ex.name}: {line}")
    sys.exit(0 if all(ok for ok, _ in results) else 1)


if __name__ == "__main__":
    main()
