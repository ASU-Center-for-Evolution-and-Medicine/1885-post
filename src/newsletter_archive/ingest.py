"""ingest(raw_bytes, to_address) -> Newsletter

This is the one seam component #1 -- whatever inbox-access mechanism ends up being
used (IMAP poller, Cloudflare Email Worker, provider webhook, ...) -- plugs into.
It only ever needs to hand over raw MIME bytes plus which archive address received
them; everything else (parsing, link neutralization, storage, slugging) happens here.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timezone
from email.utils import parseaddr
from pathlib import Path

from . import storage
from .parser import parse_email
from .sanitizer import neutralize_unsubscribe_links, rewrite_inline_image_sources
from .slug import make_slug


def ingest(raw_bytes: bytes, to_address: str, conn=None) -> storage.Newsletter:
    owns_conn = conn is None
    conn = conn or storage.connect()
    try:
        parsed = parse_email(raw_bytes)
        slug = make_slug(parsed.subject, parsed.date, parsed.message_id)

        received_at = None
        if parsed.date:
            dt = parsed.date
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            received_at = dt.isoformat()

        html = parsed.html_body
        if html:
            html = neutralize_unsubscribe_links(html)
            if parsed.inline_images:
                url_by_content_id = {
                    image.content_id: f"/n/{slug}/images/{image.content_id}"
                    for image in parsed.inline_images
                }
                html = rewrite_inline_image_sources(html, url_by_content_id)

        from_email = parseaddr(parsed.from_address)[1].lower() or None

        try:
            newsletter_id = storage.insert_newsletter(
                conn,
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
        except sqlite3.IntegrityError:
            # Same Message-ID (and therefore same slug) ingested twice -- expected under
            # at-least-once delivery (webhook retries, re-fetching the same IMAP message).
            # Treat as a no-op and return what's already archived rather than erroring.
            existing = storage.get_by_slug(conn, slug)
            if existing is not None:
                return existing
            raise

        for image in parsed.inline_images:
            storage.insert_image(
                conn,
                newsletter_id=newsletter_id,
                content_id=image.content_id,
                content_type=image.content_type,
                data=image.data,
            )

        return storage.get_by_slug(conn, slug)
    finally:
        if owns_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(description="Ingest a raw .eml file into the archive.")
    arg_parser.add_argument("eml_path", type=Path, help="Path to a raw MIME (.eml) file")
    arg_parser.add_argument(
        "--to",
        default="newsletter-archive@example.com",
        help="Archive address this email was received at (default: %(default)s)",
    )
    args = arg_parser.parse_args(argv)

    raw_bytes = args.eml_path.read_bytes()
    newsletter = ingest(raw_bytes, args.to)
    print(f"Archived: {newsletter.subject!r} -> /n/{newsletter.slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
