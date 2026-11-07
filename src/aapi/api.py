"""aapi - Anna's Archive over curl.

    curl -OJ localhost:8742/sovereign+individual         best PDF
    curl -OJ localhost:8742/sovereign+individual.epub    best EPUB  (.pdf .epub .mobi .djvu .azw3)
    curl     localhost:8742/sovereign+individual.json    candidates, grouped by type
    curl -I  localhost:8742/sovereign+individual.epub    what you'd get, without downloading
    curl -OJ localhost:8742/md5/<md5>                    one specific file

    ?lang=pl   language (default en, empty = any)
"""

import os
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import sources
from .search import TYPES, Hit, search


def _split(path: str) -> tuple[str, str]:
    """'sovereign+individual.epub' -> ('sovereign individual', 'epub')"""
    stem, dot, ext = path.rpartition(".")
    if dot and (ext in TYPES or ext == "json"):
        path = stem
    else:
        ext = "pdf"
    return path.replace("+", " ").strip(), ext


def _headers(h: Hit) -> dict:
    return {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(h.filename)}",
        "X-AA-MD5": h.md5,
        "X-AA-Title": quote(h.title, safe=" ,:'"),
        "X-AA-Author": quote(h.author, safe=" ,:'"),
        "X-AA-Meta": f"{h.ext} {h.size} {h.year} {h.lang}".strip(),
    }


def _send(md5: str, headers: dict) -> StreamingResponse:
    r = sources.open_file(md5)
    if n := r.headers.get("content-length"):
        headers["Content-Length"] = n

    def body():
        try:
            yield from r.iter_content()
        finally:
            r.close()

    return StreamingResponse(body(), media_type=r.headers.get("content-type"), headers=headers)


def query(req):
    q, ext = _split(req.path_params["path"])
    lang = req.query_params.get("lang", "en")
    if not q:
        return PlainTextResponse(__doc__)

    if ext == "json":
        grouped: dict[str, list] = {}
        for h in search(q, lang=lang):
            grouped.setdefault(h.ext or "other", []).append(h.json())
        return JSONResponse(grouped)

    hits = search(q, ext=ext, lang=lang, limit=3)
    if not hits:
        return PlainTextResponse(f"nothing for {q!r} as {ext}\n", status_code=404)
    if req.method == "HEAD":
        return Response(headers=_headers(hits[0]))
    for h in hits:  # next candidate if every source fails for this one
        try:
            return _send(h.md5, _headers(h))
        except FileNotFoundError:
            continue
    return PlainTextResponse(f"found {len(hits)} but no source serves them\n", status_code=502)


def by_md5(req):
    md5 = req.path_params["md5"]
    return _send(md5, {"Content-Disposition": f"attachment; filename={md5}"})


async def failed(req, exc):
    return PlainTextResponse(f"{type(exc).__name__}: {exc}\n", status_code=502)


app = Starlette(
    routes=[
        Route("/md5/{md5:str}", by_md5, methods=["GET", "HEAD"]),
        Route("/{path:path}", query, methods=["GET", "HEAD"]),
    ],
    exception_handlers={Exception: failed},
)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("AAPI_HOST", "127.0.0.1"), port=int(os.environ.get("AAPI_PORT", 8742)))
