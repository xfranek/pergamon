"""Obtain a DDoS-Guard session for Anna's Archive.

A headless Camoufox passes the JS challenge once; the resulting __ddg* cookies
are portable to curl_cffi's Chrome impersonation but bound to the solving IP
(__ddg9_ holds it), so they must be reused through the same exit. Exits and the
CLI live in pool.py.
"""

import json
import os
import re
import time
from pathlib import Path

from curl_cffi import requests

DOMAIN = os.environ.get("AAPI_DOMAIN", "annas-archive.gl")
STATE = Path(os.environ.get("AAPI_STATE", Path.home() / ".cache" / "aapi" / "session.json"))
REFRESH_MARGIN = 5 * 60  # re-solve when the shortest cookie has < 5 min left
PROBE = "/search?q=test"


def _solve(proxy: dict | None = None) -> dict:
    from camoufox.sync_api import Camoufox

    # os/canvas_noise matter: noisy canvas trips DDoS-Guard's "picasso" check;
    # geoip keeps timezone/locale consistent with the exit IP when proxied.
    kw = {"headless": True, "os": "macos", "canvas_noise": False, "i_know_what_im_doing": True}
    if proxy:
        kw |= {"proxy": proxy, "geoip": True}
    with Camoufox(**kw) as b:
        p = b.new_page()
        p.goto(f"https://{DOMAIN}{PROBE}", timeout=60_000)
        # Done only once results render: the transient "Loading …?check=1" title
        # appears before DDoS-Guard has validated, and cookies taken then are refused.
        try:
            p.wait_for_selector('a[href^="/md5/"]', state="attached", timeout=45_000)
        except Exception as e:
            raise RuntimeError(f"challenge not passed: {p.title()!r} {p.url}") from e
        jar = [c for c in p.context.cookies() if DOMAIN in c["domain"]]

    expiring = [c["expires"] for c in jar if c["name"].startswith("__ddg") and c["expires"] > 0]
    return {
        "domain": DOMAIN,
        "solved_at": time.time(),
        "expires_at": min(expiring) if expiring else time.time() + 15 * 60,
        "cookies": {c["name"]: c["value"] for c in jar},
    }


def load(path: Path = STATE) -> dict | None:
    try:
        s = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if s.get("domain") != DOMAIN or s["expires_at"] - time.time() < REFRESH_MARGIN:
        return None
    return s


def save(s: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def works(s: dict, proxies: dict | None = None) -> bool:
    r = requests.get(f"https://{DOMAIN}{PROBE}", cookies=s["cookies"], impersonate="chrome", timeout=30, proxies=proxies)
    return r.status_code == 200 and "DDoS-Guard" not in r.text and bool(re.search(r"/md5/[0-9a-f]{32}", r.text))
