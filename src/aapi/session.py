"""Obtain a DDoS-Guard session for Anna's Archive.

A headless Camoufox passes the JS challenge once; the resulting __ddg* cookies
are not bound to UA/TLS, so plain curl_cffi requests can reuse them until they
expire (~20 min for __ddg8_/9_/10_).

    aapi-session            # solve if needed, print cookie file path
    aapi-session --force    # always solve
    aapi-session --check    # only verify the cached session
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

DOMAIN = os.environ.get("AAPI_DOMAIN", "annas-archive.gl")
STATE = Path(os.environ.get("AAPI_STATE", Path.home() / ".cache" / "aapi" / "session.json"))
REFRESH_MARGIN = 5 * 60  # re-solve when the shortest cookie has < 5 min left
PROBE = "/search?q=test"


def _solve() -> dict:
    from camoufox.sync_api import Camoufox

    # os/canvas_noise matter: noisy canvas trips DDoS-Guard's "picasso" check.
    with Camoufox(headless=True, os="macos", canvas_noise=False, i_know_what_im_doing=True) as b:
        p = b.new_page()
        p.goto(f"https://{DOMAIN}{PROBE}", timeout=60_000)
        for _ in range(40):
            if "ddos" not in p.title().lower():
                break
            p.wait_for_timeout(1000)
        else:
            raise RuntimeError(f"challenge not passed: {p.title()!r} {p.url}")
        jar = [c for c in p.context.cookies() if DOMAIN in c["domain"]]

    expiring = [c["expires"] for c in jar if c["name"].startswith("__ddg") and c["expires"] > 0]
    return {
        "domain": DOMAIN,
        "solved_at": time.time(),
        "expires_at": min(expiring) if expiring else time.time() + 15 * 60,
        "cookies": {c["name"]: c["value"] for c in jar},
    }


def load() -> dict | None:
    try:
        s = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None
    if s.get("domain") != DOMAIN or s["expires_at"] - time.time() < REFRESH_MARGIN:
        return None
    return s


def save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.chmod(0o600)
    tmp.replace(STATE)


def works(s: dict) -> bool:
    r = requests.get(f"https://{DOMAIN}{PROBE}", cookies=s["cookies"], impersonate="chrome", timeout=30)
    return r.status_code == 200 and "DDoS-Guard" not in r.text and bool(re.search(r"/md5/[0-9a-f]{32}", r.text))


def get(force: bool = False) -> dict:
    """Return a valid session, solving the challenge only when needed."""
    s = None if force else load()
    if s is None or not works(s):
        s = _solve()
        if not works(s):
            raise RuntimeError("solved session rejected by plain requests")
        save(s)
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="always solve a fresh challenge")
    ap.add_argument("--check", action="store_true", help="only verify the cached session")
    a = ap.parse_args()

    if a.check:
        s = load()
        ok = s is not None and works(s)
        left = int(s["expires_at"] - time.time()) if s else 0
        print(f"{'ok' if ok else 'invalid'} ({left}s left)" if s else "no cached session")
        sys.exit(0 if ok else 1)

    t0 = time.time()
    s = get(force=a.force)
    print(f"{STATE}  ({int(s['expires_at'] - time.time())}s left, {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
