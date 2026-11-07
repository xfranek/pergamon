# pergamon

Anna's Archive over curl. One request, one file — built for agents.

```sh
curl -OJ localhost:8742/pride+and+prejudice.epub
```

> When Egypt cut off its papyrus, the library of Pergamon answered with parchment.

## Usage

The path is the query, the extension is the format.

```sh
curl -OJ localhost:8742/pride+and+prejudice          # best PDF
curl -OJ localhost:8742/pride+and+prejudice.epub     # best EPUB — also .mobi .djvu .azw3
curl     localhost:8742/pride+and+prejudice.json     # all candidates, grouped by format
curl -I  localhost:8742/pride+and+prejudice.epub     # the pick, without downloading
curl -OJ localhost:8742/md5/<md5>                    # one specific file
```

`?lang=pl` picks a language (default `en`, empty for any).
`HEAD` answers with `X-AA-MD5`, `X-AA-Title`, `X-AA-Author` and `X-AA-Meta`.

## Run

```sh
uv sync
uv run pergamon-session   # solve the browser check once, cache the session
uv run pergamon           # serve on 127.0.0.1:8742
```

## How it works

- **session** — a headless [Camoufox](https://camoufox.com) passes the browser check once; the cookies are reused by plain [curl_cffi](https://github.com/lexiforest/curl_cffi) requests until they expire.
- **search** — results are parsed and ranked by title match, format, language and availability.
- **sources** — the top candidates race; Anna's member API first, Libgen mirrors after. The first one that serves a real file wins.
- **pool** — optional exits through [mullid](https://github.com/xfranek/mullid) profiles. Sessions are bound to the IP that solved them, so each exit keeps its own. Requests queue per exit and answer `503` with `Retry-After` when saturated.

## Note

Anna's Archive is run by volunteers. Be gentle with it, and [donate](https://annas-archive.gl/donate) — a member key is also the fastest way to download.
