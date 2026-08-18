"""D1-backed persistence -- the Worker-side counterpart to storage.py's sqlite3 wrapper.

Same two tables, same query shapes (get_by_slug, list_newsletters, insert_newsletter,
insert_image, get_image), same idempotency requirement -- expressed here as
`ON CONFLICT(slug) DO NOTHING` instead of catching sqlite3.IntegrityError, since D1
supports it declaratively. Access is async and binding-based (`env.DB`) rather than a
local file, so this can't share storage.py's sync functions; see ingest.py's
docstring / worker_entry.py for how the two paths stay in sync on schema and behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr

_LIST_COLUMNS = "id, message_id, from_address, from_email, to_address, subject, received_at, slug, created_at"
_DETAIL_COLUMNS = _LIST_COLUMNS + ", sanitized_html, plain_text_fallback"


@dataclass
class NewsletterSummary:
    id: int
    message_id: str | None
    from_address: str
    from_email: str | None
    to_address: str
    subject: str
    received_at: str | None
    slug: str
    created_at: str


@dataclass
class Newsletter(NewsletterSummary):
    sanitized_html: str | None = None
    plain_text_fallback: str | None = None


async def insert_newsletter(
    db,
    *,
    message_id: str | None,
    from_address: str,
    from_email: str | None,
    to_address: str,
    subject: str,
    received_at: str | None,
    slug: str,
    raw_eml: bytes,
    sanitized_html: str | None,
    plain_text_fallback: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare(
            """
            INSERT INTO newsletters
                (message_id, from_address, from_email, to_address, subject, received_at, slug,
                 raw_eml, sanitized_html, plain_text_fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO NOTHING
            """
        )
        .bind(
            message_id,
            from_address,
            from_email,
            to_address,
            subject,
            received_at,
            slug,
            raw_eml,
            sanitized_html,
            plain_text_fallback,
            now,
        )
        .run()
    )


async def insert_image(db, *, newsletter_id: int, content_id: str, content_type: str, data: bytes) -> None:
    await (
        db.prepare(
            """
            INSERT INTO newsletter_images (newsletter_id, content_id, content_type, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(newsletter_id, content_id) DO NOTHING
            """
        )
        .bind(newsletter_id, content_id, content_type, data)
        .run()
    )


async def get_id_by_slug(db, slug: str) -> int | None:
    row = await db.prepare("SELECT id FROM newsletters WHERE slug = ?").bind(slug).first()
    return row["id"] if row else None


async def get_by_slug(db, slug: str) -> Newsletter | None:
    row = await db.prepare(f"SELECT {_DETAIL_COLUMNS} FROM newsletters WHERE slug = ?").bind(slug).first()
    return _row_to_newsletter(row) if row else None


async def get_image(db, slug: str, content_id: str) -> tuple[str, bytes] | None:
    row = await (
        db.prepare(
            """
            SELECT ni.content_type, ni.data
            FROM newsletter_images ni
            JOIN newsletters n ON n.id = ni.newsletter_id
            WHERE n.slug = ? AND ni.content_id = ?
            """
        )
        .bind(slug, content_id)
        .first()
    )
    return (row["content_type"], row["data"]) if row else None


async def list_admin_senders(db, user_email: str) -> list[str]:
    """Sender (from_email) addresses this user administers -- grants delete rights over
    newsletters from that sender, nothing else. Every authenticated user can already
    *view* every newsletter; this is a smaller grant on top of that, not a replacement.
    Scoped by sender rather than to_address: a single-inbox archive has one to_address
    shared by every newsletter, so it was never a meaningful axis to grant rights over."""
    result = (
        await db.prepare("SELECT from_email FROM newsletter_admins WHERE user_email = ?").bind(user_email).all()
    )
    return [row["from_email"] for row in result.results]


@dataclass
class AdminGrant:
    id: int
    user_email: str
    from_email: str
    created_at: str


async def list_admin_grants(db) -> list[AdminGrant]:
    result = await db.prepare(
        "SELECT id, user_email, from_email, created_at FROM newsletter_admins ORDER BY user_email, from_email"
    ).all()
    return [
        AdminGrant(id=r["id"], user_email=r["user_email"], from_email=r["from_email"], created_at=r["created_at"])
        for r in result.results
    ]


async def add_admin_grant(db, user_email: str, from_email: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare(
            "INSERT INTO newsletter_admins (user_email, from_email, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_email, from_email) DO NOTHING"
        )
        .bind(user_email, from_email, now)
        .run()
    )


async def delete_admin_grant(db, grant_id: int) -> None:
    await db.prepare("DELETE FROM newsletter_admins WHERE id = ?").bind(grant_id).run()


async def delete_newsletter(db, slug: str) -> bool:
    newsletter_id = await get_id_by_slug(db, slug)
    if newsletter_id is None:
        return False
    await db.prepare("DELETE FROM newsletter_images WHERE newsletter_id = ?").bind(newsletter_id).run()
    await db.prepare("DELETE FROM newsletters WHERE id = ?").bind(newsletter_id).run()
    return True


async def update_received_at(db, slug: str, received_at: str) -> None:
    """Retroactively set a newsletter's send date -- for backfilling the archive with
    old issues under their original date rather than whenever they happened to be
    ingested."""
    await db.prepare("UPDATE newsletters SET received_at = ? WHERE slug = ?").bind(received_at, slug).run()


@dataclass
class SenderSummary:
    from_email: str
    name: str
    count: int


async def list_senders(db) -> list[SenderSummary]:
    result = await db.prepare(
        "SELECT from_email, MAX(from_address) AS from_address, COUNT(*) AS count "
        "FROM newsletters WHERE from_email IS NOT NULL "
        "GROUP BY from_email ORDER BY from_email"
    ).all()
    return [
        SenderSummary(
            from_email=r["from_email"],
            name=parseaddr(r["from_address"])[0] or r["from_email"],
            count=r["count"],
        )
        for r in result.results
    ]


async def list_newsletters(
    db,
    *,
    sender: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> list[NewsletterSummary]:
    where = ""
    params: list[str] = []
    if sender:
        where = "WHERE from_email = ?"
        params.append(sender)
    direction = "ASC" if sort == "oldest" else "DESC"
    result = await (
        db.prepare(
            f"SELECT {_LIST_COLUMNS} FROM newsletters {where} "
            f"ORDER BY received_at {direction}, id {direction} LIMIT ? OFFSET ?"
        )
        .bind(*params, limit, offset)
        .all()
    )
    return [_row_to_summary(row) for row in result.results]


def _row_to_summary(row) -> NewsletterSummary:
    return NewsletterSummary(
        id=row["id"],
        message_id=row["message_id"],
        from_address=row["from_address"],
        from_email=row["from_email"],
        to_address=row["to_address"],
        subject=row["subject"],
        received_at=row["received_at"],
        slug=row["slug"],
        created_at=row["created_at"],
    )


def _row_to_newsletter(row) -> Newsletter:
    return Newsletter(
        **_row_to_summary(row).__dict__,
        sanitized_html=row["sanitized_html"],
        plain_text_fallback=row["plain_text_fallback"],
    )
