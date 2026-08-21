# Newsletter Archive

Turns an incoming newsletter email into a permanent, linkable archive page. Deployed as
a single Cloudflare Python Worker: Cloudflare Email Routing triggers ingestion directly
(no separate relay), D1 stores archived newsletters, and the same Worker serves the
archive pages.

**Live:** https://newsletter-archive.suhail-ghafoor8737.workers.dev (account:
`Suhail.ghafoor@asu.edu's Account`, id `3ed0332b4053248f9a25eedf3c741b54`)

This README is for developing/deploying the code. For how colleagues actually use the
archive (getting a newsletter in, embedding a widget on a department site, requesting
admin access), see [USAGE.md](USAGE.md).

## Architecture

- `src/newsletter_archive/parser.py`, `sanitizer.py`, `slug.py` — pure logic, no I/O:
  MIME parsing (stdlib `email`), unsubscribe/preferences link neutralization
  (BeautifulSoup + `html.parser`), permalink slug generation. Shared by both paths below.
- `src/newsletter_archive/web/app.py` — the FastAPI app, served inside the Worker via
  its built-in ASGI adapter. Routes: `/` (list + date/sender/subject filters),
  `/n/{slug}` (permalink page), `/n/{slug}/images/{content_id}`, `POST /ingest`.
- `src/newsletter_archive/storage_d1.py` — async, D1-backed storage used by the deployed
  routes and the email handler.
- `src/worker_entry.py` — the Worker entrypoint: `fetch` hands off to the FastAPI app;
  `email` (Cloudflare Email Routing's inbound trigger) reads the raw MIME and calls
  `ingest_via_d1()`, which runs the same parse → sanitize → slug → store pipeline.
- `src/newsletter_archive/storage.py` + `ingest.py` + the CLI (`python -m
  newsletter_archive.ingest`) — a separate, sync/sqlite3 path used only by the pytest
  suite as a fast, wrangler-free way to test the shared parsing/sanitizing logic. Not
  part of the deployed Worker.

Everything under `src/` is what the Worker bundler walks and deploys; `tests/`,
`migrations/`, and local venvs deliberately live outside it (see the bundle-size note
below for why that boundary matters).

## Local development

Local dev and the deployed Worker **share one D1 database** (`d1_databases[0].remote:
true` in `wrangler.jsonc`) — there's no separate local-only copy to fall out of sync.
Ingesting a real email in production is immediately visible to (and re-testable by) a
locally-run, possibly-newer version of this code, and vice versa.

```bash
uv sync --extra dev              # sqlite3-backed pytest suite (parser/sanitizer/slug logic)
uv run pytest

npm install -g wrangler@latest   # pywrangler proxies to this; needs >=4.109.0 for Python Workers.
                                  # Deliberately global, not a project-local node_modules/ -- see
                                  # the bundle-size note below.
uv run pywrangler dev            # real Worker runtime, bound to the SAME remote D1 as production
```

With `pywrangler dev` running (defaults to `http://localhost:8787`):

```bash
curl -X POST "http://localhost:8787/ingest?to=newsletter-archive@example.com" \
  --data-binary @tests/fixtures/mailchimp_style.eml \
  -H "Content-Type: message/rfc822"
```

then open the printed `/n/{slug}` URL, or `http://localhost:8787/` for the list/filter
view -- this write is real and immediately visible at the production URL too. Re-posting
the same email (same `Message-ID`) is a no-op — it returns the existing entry instead of
erroring, since Email Routing retries and other push sources can redeliver.

Set `NEWSLETTER_ARCHIVE_INGEST_TOKEN` (checked against an `X-Ingest-Token` header on
`POST /ingest`) before this Worker is reachable by anything other than you.

## Deploying

```bash
uv run pywrangler d1 execute DB --remote --file migrations/0001_init.sql   # first time / schema changes only
uv run pywrangler deploy
```

Still to do: in the Cloudflare dashboard (or via `wrangler email routing rules create`),
route an inbound address to this Worker. See `wrangler email routing --help`.

## What gets changed vs. preserved

Only unsubscribe / manage-preferences / email-preferences links (matched by anchor text
and known href patterns, see `sanitizer.py`) get their `href` replaced with `#`. Every
other link and all other content is left exactly as it was in the original email. Inline
(`cid:`) images are extracted and re-served from the archive so permalinks don't show
broken images; externally-hosted images are left untouched.

## Notes on the Python Workers port

Python Workers are still in **open beta**. A few things worth knowing if you're
modifying this:

- FastAPI runs via a built-in ASGI adapter (`import asgi; await asgi.fetch(app,
  request.js_object, self.env)`), not Uvicorn — there's no persistent server process.
- Jinja2 templates are in-memory strings (`jinja2.Environment(loader=jinja2.DictLoader(...))`
  in `web/app.py`) rather than `Jinja2Templates(directory=...)` — this mirrors
  Cloudflare's own FastAPI example, which uses the same in-memory pattern rather than
  file-based template loading under the bundled Pyodide filesystem.
- D1 access is async and binding-based: `await env.DB.prepare(sql).bind(...).run()` /
  `.first()` / `.all()` (see `storage_d1.py`), not the `sqlite3` module.
- `message.raw` (in the `email` handler) is a JS `ReadableStream` with no Python
  `.bytes()` wrapper; it's read via `(await js.Response.new(message.raw).arrayBuffer()).to_bytes()`,
  the same technique the `workers` SDK uses internally for reading response bodies.
- **Bundle size / project layout matters a lot.** The Python bundler walks the entry
  file's directory for modules to vendor (no tree-shaking, and neither `.gitignore` nor
  `.wranglerignore` is consulted). With everything at the project root, local venvs
  (`.venv`, `.venv-workers`, and its nested `pyodide-venv`) and a project-local
  `node_modules/` all got swept into the deploy bundle -- 8.7MB gzip, which failed
  outright on this account's free-tier 3MB limit (`wrangler deploy --dry-run` computes
  the real size but does *not* check the account's plan-tier limit, so this only surfaced
  on a real `deploy`, not the dry run). Moving the entry point and package under `src/`
  (matching Cloudflare's own examples) and keeping `wrangler` a global npm install
  dropped it to **2.4MB gzip** by excluding everything outside `src/` from the walk.
  If this ever creeps back up: check `wrangler deploy` output for "largest dependencies"
  first, don't assume `--dry-run` alone proves a deploy will succeed.
- `wrangler.jsonc` pins `account_id` explicitly (`3ed0332b4053248f9a25eedf3c741b54`,
  `Suhail.ghafoor@asu.edu's Account`) since this Cloudflare login has access to multiple
  accounts and `wrangler`/`pywrangler` would otherwise need `--account-id` on every call.
