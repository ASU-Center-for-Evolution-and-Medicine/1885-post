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
from datetime import timezone
from email.utils import parseaddr

import js
from workers import WorkerEntrypoint
from workers import fetch as workers_fetch

from newsletter_archive import storage_d1 as storage
from newsletter_archive.parser import parse_email
from newsletter_archive.sanitizer import (
    find_trackable_links,
    neutralize_unsubscribe_links,
    rewrite_inline_image_sources,
    rewrite_tracked_links,
)
from newsletter_archive.slug import make_slug
from newsletter_archive.web.app import app


async def resolve_link(url: str) -> str:
    """Follow a tracked link's redirect chain to its real, permanent destination.

    Fails open (keeps the original tracked URL) on any error -- a live tracked link
    beats a rewrite gone wrong. No explicit timeout: Workers already enforces its own
    platform-level subrequest limits, so a hand-rolled AbortController timeout isn't
    needed to avoid hanging the request.
    """
    try:
        resp = await workers_fetch(url)
        return resp.url or url
    except Exception:
        return url


async def _build_sanitized_html(parsed, slug: str) -> str | None:
    """The full body-rewrite pipeline, shared by fresh ingestion and reprocessing an
    already-archived newsletter's stored raw_eml: resolve tracked links to their real
    destination *before* classifying, since a tracked href is opaque and reveals nothing
    about whether it's really an unsubscribe/preference-center link."""
    html = parsed.html_body
    if not html:
        return None

    tracked = find_trackable_links(html)
    if tracked:
        resolved = dict(zip(tracked, await asyncio.gather(*(resolve_link(url) for url in tracked))))
        html = rewrite_tracked_links(html, resolved)

    html = neutralize_unsubscribe_links(html)

    if parsed.inline_images:
        url_by_content_id = {
            image.content_id: f"/n/{slug}/images/{image.content_id}" for image in parsed.inline_images
        }
        html = rewrite_inline_image_sources(html, url_by_content_id)

    return html


async def ingest_via_d1(raw_bytes: bytes, to_address: str, db) -> storage.Newsletter:
    parsed = parse_email(raw_bytes)
    slug = make_slug(parsed.subject, parsed.date, parsed.message_id)

    received_at = None
    if parsed.date:
        dt = parsed.date
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        received_at = dt.isoformat()

    html = await _build_sanitized_html(parsed, slug)
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


async def reprocess_via_d1(slug: str, db) -> storage.Newsletter | None:
    """Re-run the parse/resolve/sanitize pipeline against a newsletter's already-stored
    raw_eml and overwrite sanitized_html in place -- same row, same slug, nothing else
    touched. For backfilling newsletters ingested before a sanitizer change (e.g. link
    resolution) while their tracked links are still live, and useful going forward
    whenever sanitizer rules evolve again."""
    stored = await storage.get_raw_eml(db, slug)
    if stored is None:
        return None
    raw_eml, _to_address = stored
    raw_eml = bytes(raw_eml)

    parsed = parse_email(raw_eml)
    html = await _build_sanitized_html(parsed, slug)
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
        await ingest_via_d1(raw_bytes, message.to, self.env.DB)
