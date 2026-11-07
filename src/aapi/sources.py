"""Resolve an md5 to a direct file URL: Anna's member API first, Libgen mirrors after."""

import html
import os
import re
from collections.abc import Callable, Iterator

from curl_cffi import requests

from . import session

ANNAS_KEY = os.environ.get("ANNAS_KEY", "")
LIBGEN = os.environ.get("AAPI_LIBGEN", "libgen.la,libgen.li,libgen.gl,libgen.bz").split(",")


def annas(md5: str) -> Iterator[str]:
    if not ANNAS_KEY:
        return
    r = requests.get(f"https://{session.DOMAIN}/dyn/api/fast_download.json",
                     params={"md5": md5, "key": ANNAS_KEY}, impersonate="chrome", timeout=30)
    if r.status_code == 200 and (url := r.json().get("download_url")):
        yield url


def libgen(md5: str) -> Iterator[str]:
    # ads.php carries a short-lived ?key= on its GET button
    for host in LIBGEN:
        try:
            r = requests.get(f"https://{host}/ads.php?md5={md5}", impersonate="chrome", timeout=20)
        except requests.RequestsError:
            continue
        if m := re.search(r"get\.php\?md5=[0-9a-f]{32}&(?:amp;)?key=\w+", r.text):
            yield f"https://{host}/{html.unescape(m.group(0))}"


RESOLVERS: list[Callable[[str], Iterator[str]]] = [annas, libgen]


def open_file(md5: str) -> requests.Response:
    """First source that actually serves a file (not an HTML error page)."""
    for resolve in RESOLVERS:
        for url in resolve(md5):
            r = requests.get(url, impersonate="chrome", timeout=120, stream=True)
            if r.status_code == 200 and not r.headers.get("content-type", "").startswith("text/html"):
                return r
            r.close()
    raise FileNotFoundError(f"no source serves {md5}")
