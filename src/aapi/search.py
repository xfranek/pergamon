"""Anna's Archive search: fetch, parse, rank."""

import html
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode

from curl_cffi import requests

from . import pool, session

TYPES = {"pdf": 3, "epub": 2, "djvu": 1, "mobi": 1, "azw3": 1}  # value = preference when type is open


@dataclass
class Hit:
    md5: str
    title: str = ""
    author: str = ""
    ext: str = ""
    size: str = ""
    year: str = ""
    lang: str = ""
    sources: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def filename(self) -> str:
        author = re.split(r",|;| and | & ", self.author)[0].strip()  # first author only
        title = self.title.split(":")[0].strip()[:120]                # drop the subtitle
        name = " - ".join(p for p in (author, title) if p) or self.md5
        name += f" ({self.year})" if self.year else ""
        name = re.sub(r'\s+', " ", re.sub(r'[/\\:*?"<>|]+', " ", name)).strip()
        return name + (f".{self.ext}" if self.ext else "")

    def json(self) -> dict:
        return asdict(self) | {"filename": self.filename}


# --- fetch -------------------------------------------------------------------

FAILOVER = 2  # exits to try before giving up


def _page(path: str) -> str:
    errors = []
    for ex in pool.exits()[:FAILOVER]:
        try:
            return _page_via(ex, path)
        except (RuntimeError, ConnectionError, requests.RequestsError) as e:
            errors.append(f"{ex.name}: {e}")
    raise ConnectionError("; ".join(errors))


def _page_via(ex: pool.Exit, path: str) -> str:
    # cookies only work through the exit that minted them
    s = ex.session()
    for retry in (False, True):
        if retry:
            s = ex.session(refused=s["solved_at"])
        r = requests.get(f"https://{session.DOMAIN}{path}", cookies=s["cookies"],
                         impersonate="chrome", timeout=30, proxies=ex.proxies)
        if r.status_code == 200 and "DDoS-Guard" not in r.text:
            return r.text
    raise ConnectionError(f"session refused ({r.status_code})")


# --- parse -------------------------------------------------------------------

_BLOCK = re.compile(r'<div class="flex\s+pt-3 pb-3 border-b.*?(?=<div class="flex\s+pt-3 pb-3 border-b|$)', re.S)
_MD5 = re.compile(r'href="/md5/([0-9a-f]{32})"')
_TITLE = re.compile(r'href="/md5/[0-9a-f]{32}"[^>]*font-semibold[^>]*>(.*?)</a>', re.S)
_AUTHOR = re.compile(r'mdi--user-edit\][^>]*></span>(.*?)</a>', re.S)
_META = re.compile(r'<div class="text-gray-800[^"]*font-semibold text-sm[^"]*"[^>]*>(.*?)<script', re.S)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _first(pattern: str, parts: list[str]) -> str:
    """First part matching pattern; its group 1 if it has one."""
    return next((m.group(m.lastindex or 0) for p in parts if (m := re.fullmatch(pattern, p))), "")


def _hit(block: str) -> Hit | None:
    if not (md5 := _MD5.search(block)):
        return None
    grab = lambda rx: _text(m.group(1)) if (m := rx.search(block)) else ""
    # meta line: "English [en] · PDF · 2.8MB · 2016 · 📘 Book (non-fiction) · 🚀/lgli/zlib · Save"
    parts = [p.strip() for p in grab(_META).split("·")]
    return Hit(
        md5=md5.group(1),
        title=grab(_TITLE),
        author=grab(_AUTHOR),
        ext=_first(r"[A-Z0-9]{2,5}", parts).lower(),
        size=_first(r"[\d.]+\s?[KMG]B", parts),
        year=_first(r"(?:1[5-9]|20)\d\d", parts),
        lang=_first(r".*\[(\w+)\]", parts),
        sources=[s for s in _first(r"🚀/(.+)", parts).split("/") if s],
    )


def parse(page: str) -> list[Hit]:
    page = page.split("js-partial-matches", 1)[0]  # drop the "partial matches" tail
    hits, seen = [], set()
    for block in _BLOCK.findall(page):
        if (h := _hit(block)) and h.md5 not in seen:
            seen.add(h.md5)
            hits.append(h)
    return hits


# --- rank --------------------------------------------------------------------

def _words(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 2}


def _mb(size: str) -> float:
    m = re.match(r"([\d.]+)\s?([KMG])B", size)
    return float(m.group(1)) * {"K": 2**-10, "M": 1, "G": 2**10}[m.group(2)] if m else 0.0


def _score(h: Hit, query: str, rank: int, lang: str) -> float:
    q, t = _words(query), _words(h.title)
    s = 10 * len(q & (t | _words(h.author))) / max(len(q), 1)            # relevance: query covered
    s += 4 * len(q & t) / max(len(t), 1)                                  # precision: title isn't mostly other words
    s += TYPES.get(h.ext, 0)                                              # format preference
    s += 2 * (h.lang == lang) + 0.3 * len(h.sources)                       # language, availability
    s -= 0.1 * rank                                                       # trust Anna's order a bit
    s -= 5 * (0 < _mb(h.size) < 0.1)                                      # stubs / broken files
    return round(s, 2)


def search(query: str, ext: str = "", lang: str = "en", limit: int = 20) -> list[Hit]:
    params = {"q": query} | ({"ext": ext} if ext else {}) | ({"lang": lang} if lang else {})
    hits = parse(_page(f"/search?{urlencode(params)}"))
    for rank, h in enumerate(hits):
        h.score = _score(h, query, rank, lang)
    return sorted(hits, key=lambda h: -h.score)[:limit]
