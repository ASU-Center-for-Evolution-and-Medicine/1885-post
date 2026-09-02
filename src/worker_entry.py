"""Cloudflare Python Worker entrypoint.

Two triggers share one deployed Worker:
- `fetch`: serves the FastAPI app (newsletter_archive.web.app) via the Workers-provided
  ASGI adapter -- the permalink/list pages and the POST /ingest testing route.
- `email`: Cloudflare Email Routing's inbound trigger. Reads the raw MIME bytes and
  runs them through the same parse/sanitize/slug pipeline as ingest.py's sync CLI path,
  writing to D1 instead of sqlite3 via ingest_via_d1() below.

ingest_via_d1() is the async, D1-backed counterpart to ingest.ingest() (newsletter_archive/
ingest.py) -- same shared parser/sanitizer/slug functions, different (async, binding-based)
storage. See that module's docstring and the plan for why these stayed separate instead of
one function supporting both a sync and an async storage backend.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timezone
from email.utils import parseaddr

import js
from js import Object
from pyodide.ffi import to_js
from workers import WorkerEntrypoint
from workers import fetch as workers_fetch

from newsletter_archive import storage_d1 as storage
from newsletter_archive.parser import parse_email
from newsletter_archive.sanitizer import (
    find_external_css_images,
    find_external_images,
    find_trackable_links,
    neutralize_unsubscribe_links,
    rewrite_css_image_urls,
    rewrite_external_images,
    rewrite_inline_image_sources,
    rewrite_tracked_links,
)
from newsletter_archive.slug import make_slug
from newsletter_archive.web.app import app


# Cloudflare Workers caps total subrequests (fetch calls + D1 queries) per invocation --
# confirmed in production ("AbortError: Too many subrequests by single Worker
# invocation") on a newsletter with ~24 tracked links. Concurrency throttling alone
# doesn't help (the cap is cumulative, not simultaneous), so new resolutions are also
# capped per run; get_resolved_links/save_resolved_links (storage_d1.py) make repeat
# runs incremental instead of restarting from zero and hitting the same wall every time.
# Shared by both tracked-link resolution and image mirroring below -- both draw on the
# same "external subrequest" budget within one invocation, R2 head/put calls don't (they
# come out of a separate, much larger "Cloudflare services" budget).
_MAX_CONCURRENT_RESOLUTIONS = 6
_MAX_NEW_RESOLUTIONS_PER_RUN = 20
_MAX_NEW_ASSET_FETCHES_PER_RUN = 20
_MIN_MIRRORED_IMAGE_BYTES = 200  # below this, treat as a same-shape tracking pixel

# Bulk backfill (see backfill_batch): a single request works through many newsletters,
# so the two per-newsletter caps above (worst case 20 + 20 = 40) aren't enough on their
# own -- a handful of link/image-heavy newsletters in one batch could still add up past
# the external-fetch limit. _FetchBudget is shared across every newsletter in a batch so
# the batch stops admitting new work before that happens, rather than per newsletter.
_BULK_FETCH_BUDGET = 35
_MAX_NEWSLETTERS_PER_BATCH = 30


class _FetchBudget:
    """Mutable shared cap on new external fetches across an entire bulk-backfill batch
    (as opposed to _MAX_NEW_RESOLUTIONS_PER_RUN/_MAX_NEW_ASSET_FETCHES_PER_RUN, which cap
    a single newsletter). `take()` hands out at most what's left, so a request/CSS-image
    pass that asks for its usual 20 only gets however much of that 20 the batch can still
    afford."""

    def __init__(self, remaining: int):
        self.remaining = remaining

    def take(self, requested: int) -> int:
        n = max(0, min(requested, self.remaining))
        self.remaining -= n
        return n


async def resolve_link(url: str, semaphore: asyncio.Semaphore) -> str:
    """Follow a tracked link's redirect chain to its real, permanent destination.

    Fails open (keeps the original tracked URL) on any error -- a live tracked link
    beats a rewrite gone wrong. No explicit timeout: Workers already enforces its own
    platform-level subrequest limits, so a hand-rolled AbortController timeout isn't
    needed to avoid hanging the request.
    """
    async with semaphore:
        try:
            resp = await workers_fetch(url)
            return resp.url or url
        except Exception as exc:
            print(f"resolve_link failed for {url!r}: {exc!r}")
            return url


async def _resolve_tracked_links_in(html: str, db, budget: _FetchBudget | None = None) -> str:
    tracked = find_trackable_links(html)
    if not tracked:
        return html

    cached = await storage.get_resolved_links(db, list(tracked))
    not_cached = [url for url in tracked if url not in cached][:_MAX_NEW_RESOLUTIONS_PER_RUN]
    # Ask the shared batch budget for only what this newsletter can actually use, not a
    # blind _MAX_NEW_RESOLUTIONS_PER_RUN -- otherwise a newsletter with e.g. 3 candidates
    # would reserve (and waste) budget for 20, starving whatever comes after it in the
    # same bulk-backfill batch.
    cap = budget.take(len(not_cached)) if budget else len(not_cached)
    to_resolve = not_cached[:cap]

    resolved = dict(cached)
    if to_resolve:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RESOLUTIONS)
        results = await asyncio.gather(*(resolve_link(url, semaphore) for url in to_resolve))
        newly_resolved = {}
        for url, result in zip(to_resolve, results):
            resolved[url] = result
            if result != url:  # only persist genuine successes, not fail-open no-ops
                newly_resolved[url] = result
        await storage.save_resolved_links(db, newly_resolved)

    return rewrite_tracked_links(html, resolved)


def _asset_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _asset_url(slug: str, key: str) -> str:
    """Served by view_mirrored_asset (web/app.py), under /static/* rather than /n/{slug}/*
    so a mirrored image inherits whatever already makes /static/style.css and
    /static/app-mark.png reachable from a public embed, instead of needing an Access
    Bypass rule of its own."""
    return f"/static/newsletters/{slug}/{key}"


async def mirror_external_image(
    url: str, slug: str, bucket, semaphore: asyncio.Semaphore
) -> tuple[str, str] | None:
    """Fetch an externally-hosted image once and store it permanently in R2, so the
    newsletter keeps rendering correctly even if the original ESP-hosted copy later
    goes away. Only called for URLs not already found in mirrored_assets by
    _mirror_external_assets_in. Returns (local_path, content_type) on success.

    Fails open (returns None, leaving the original external URL in place) on any error
    or validation failure -- a live external image beats a broken rewrite, same
    philosophy as resolve_link above. Validates status/content-type/size rather than
    trusting the URL, since a dead ESP asset can return a 200 HTML error page, and a
    same-shaped-but-tiny response is almost certainly a tracking pixel rather than
    content worth preserving forever.
    """
    digest = _asset_digest(url)
    async with semaphore:
        try:
            resp = await workers_fetch(url)
            if not resp.ok:
                return None
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                return None
            body = await resp.bytes()
            if len(body) < _MIN_MIRRORED_IMAGE_BYTES:
                return None
            await bucket.put(
                f"newsletters/{slug}/{digest}",
                body,
                to_js({"httpMetadata": {"contentType": content_type}}, dict_converter=Object.fromEntries),
            )
            return _asset_url(slug, digest), content_type
        except Exception as exc:
            print(f"mirror_external_image failed for {url!r}: {exc!r}")
            return None


async def _mirror_external_assets_in(html: str, slug: str, db, bucket, budget: _FetchBudget | None = None) -> str:
    """Mirror externally-hosted <img> and CSS background-image sources into R2 and
    rewrite the HTML to point at the mirrored copies. mirrored_assets (storage_d1.py) is
    both the cache -- a repeat ingest/Reprocess run skips anything an earlier run already
    mirrored -- and the permanent provenance record of which source URL each mirrored
    image came from."""
    candidates = sorted(find_external_images(html) | find_external_css_images(html))
    if not candidates:
        return html

    cached = await storage.get_mirrored_assets(db, slug, candidates)
    url_to_path = {url: _asset_url(slug, key) for url, key in cached.items()}

    not_cached = [url for url in candidates if url not in cached][:_MAX_NEW_ASSET_FETCHES_PER_RUN]
    # Same reasoning as _resolve_tracked_links_in: request only what this newsletter can
    # actually use from the shared batch budget, not the blind per-run cap.
    cap = budget.take(len(not_cached)) if budget else len(not_cached)
    to_fetch = not_cached[:cap]

    if to_fetch:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RESOLUTIONS)
        results = await asyncio.gather(*(mirror_external_image(url, slug, bucket, semaphore) for url in to_fetch))
        newly_mirrored: list[tuple[str, str, str]] = []
        for url, result in zip(to_fetch, results):
            if result is None:
                continue
            path, content_type = result
            url_to_path[url] = path
            newly_mirrored.append((url, path.rsplit("/", 1)[-1], content_type))
        await storage.save_mirrored_assets(db, slug, newly_mirrored)

    html = rewrite_external_images(html, url_to_path)
    html = rewrite_css_image_urls(html, url_to_path)
    return html


async def _build_sanitized_html(
    parsed, slug: str, db, bucket, budget: _FetchBudget | None = None
) -> str | None:
    """The full body-rewrite pipeline, shared by fresh ingestion and reprocessing an
    already-archived newsletter's stored raw_eml: resolve tracked links to their real
    destination *before* classifying, since a tracked href is opaque and reveals nothing
    about whether it's really an unsubscribe/preference-center link. `budget` is only
    passed by backfill_batch, sharing one external-fetch cap across many newsletters
    processed in the same request -- a single ingest/Reprocess call is unaffected."""
    html = parsed.html_body
    if not html:
        return None

    html = await _resolve_tracked_links_in(html, db, budget)
    html = neutralize_unsubscribe_links(html)
    html = await _mirror_external_assets_in(html, slug, db, bucket, budget)

    if parsed.inline_images:
        url_by_content_id = {
            image.content_id: f"/n/{slug}/images/{image.content_id}" for image in parsed.inline_images
        }
        html = rewrite_inline_image_sources(html, url_by_content_id)

    return html


async def ingest_via_d1(raw_bytes: bytes, to_address: str, db, bucket) -> storage.Newsletter:
    parsed = parse_email(raw_bytes)
    slug = make_slug(parsed.subject, parsed.date, parsed.message_id)

    received_at = None
    if parsed.date:
        dt = parsed.date
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        received_at = dt.isoformat()

    html = await _build_sanitized_html(parsed, slug, db, bucket)
    from_email = parseaddr(parsed.from_address)[1].lower() or None

    await storage.insert_newsletter(
        db,
        message_id=parsed.message_id,
        from_address=parsed.from_address,
        from_email=from_email,
        to_address=to_address,
        subject=parsed.subject,
        received_at=received_at,
        slug=slug,
        raw_eml=raw_bytes,
        sanitized_html=html,
        plain_text_fallback=parsed.text_body,
    )

    newsletter_id = await storage.get_id_by_slug(db, slug)
    for image in parsed.inline_images:
        await storage.insert_image(
            db,
            newsletter_id=newsletter_id,
            content_id=image.content_id,
            content_type=image.content_type,
            data=image.data,
        )

    return await storage.get_by_slug(db, slug)


async def reprocess_via_d1(
    slug: str, db, bucket, budget: _FetchBudget | None = None
) -> storage.Newsletter | None:
    """Re-run the parse/resolve/sanitize pipeline against a newsletter's already-stored
    raw_eml and overwrite sanitized_html in place -- same row, same slug, nothing else
    touched. For backfilling newsletters ingested before a sanitizer change (e.g. link
    resolution, or external-image mirroring) while their externally-hosted content is
    still live, and useful going forward whenever sanitizer rules evolve again."""
    stored = await storage.get_raw_eml(db, slug)
    if stored is None:
        return None
    raw_eml, _to_address = stored
    raw_eml = bytes(raw_eml)

    parsed = parse_email(raw_eml)
    html = await _build_sanitized_html(parsed, slug, db, bucket, budget)
    await storage.update_sanitized_html(db, slug, html)

    newsletter_id = await storage.get_id_by_slug(db, slug)
    for image in parsed.inline_images:
        await storage.insert_image(
            db,
            newsletter_id=newsletter_id,
            content_id=image.content_id,
            content_type=image.content_type,
            data=image.data,
        )

    return await storage.get_by_slug(db, slug)


async def backfill_batch(db, bucket, after_id: int) -> dict:
    """Reprocess up to _MAX_NEWSLETTERS_PER_BATCH newsletters (id-ordered, resuming after
    `after_id` -- how the admin UI carries a cursor across separate requests/clicks),
    sharing one _BULK_FETCH_BUDGET across all of them. Cheap to call repeatedly: a
    newsletter that's already fully mirrored/resolved costs one batched D1 lookup per
    pass and no new fetches at all, so most of a batch's real budget goes toward
    newsletters that still need work rather than re-checking already-done ones.

    A newsletter unusually heavy in both tracked links and external images could in
    theory need more than one batch's whole budget to fully finish in a single pass --
    it's still marked processed and the cursor still advances past it, so a fully
    finished bulk run doesn't loop forever; anything not fully caught up gets the rest
    of the way there the next time this batch (or a plain "Reprocess links" click on that
    one newsletter) reaches it.
    """
    budget = _FetchBudget(_BULK_FETCH_BUDGET)
    rows = await storage.list_slugs_after(db, after_id, limit=_MAX_NEWSLETTERS_PER_BATCH)

    processed: list[str] = []
    last_id = after_id
    for newsletter_id, slug in rows:
        if budget.remaining <= 0:
            break
        await reprocess_via_d1(slug, db, bucket, budget)
        processed.append(slug)
        last_id = newsletter_id

    done = not await storage.list_slugs_after(db, last_id, limit=1)
    return {"processed": processed, "last_id": last_id, "done": done}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)

    async def email(self, message, env, ctx):
        # The runtime calls this positionally as email(message, env, ctx) -- dropping
        # the params entirely caused "takes 2 positional arguments but 4 were given".
        # But the positional `env` itself comes through as None, so bindings are read
        # from self.env instead (set in WorkerEntrypoint.__init__, same as fetch() above).
        #
        # message.raw is a JS ReadableStream; there's no Python wrapper with a .bytes()
        # convenience method for it (unlike Request/Blob), so read it the same way
        # workers-py's own Response body reading does: via a throwaway js.Response.
        raw_bytes = (await js.Response.new(message.raw).arrayBuffer()).to_bytes()
        await ingest_via_d1(raw_bytes, message.to, self.env.DB, self.env.ASSETS)
