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

_LIST_COLUMNS = (
    "id, message_id, from_address, from_email, to_address, subject, received_at, slug, created_at, "
    "thumbnail_key, quarantined_at, deleted_at, deleted_by"
)
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
    thumbnail_key: str | None = None
    quarantined_at: str | None = None
    deleted_at: str | None = None
    deleted_by: str | None = None


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
    thumbnail_key: str | None = None,
    quarantined_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare(
            """
            INSERT INTO newsletters
                (message_id, from_address, from_email, to_address, subject, received_at, slug,
                 raw_eml, sanitized_html, plain_text_fallback, thumbnail_key, quarantined_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            thumbnail_key,
            quarantined_at,
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


async def get_raw_eml(db, slug: str) -> tuple[bytes, str] | None:
    """(raw_eml, to_address) for reprocessing -- raw_eml is deliberately excluded from
    _LIST_COLUMNS/_DETAIL_COLUMNS so normal page views don't load that blob; this is a
    narrow query just for the reprocess path."""
    row = await db.prepare("SELECT raw_eml, to_address FROM newsletters WHERE slug = ?").bind(slug).first()
    return (row["raw_eml"], row["to_address"]) if row else None


async def update_sanitized_html(
    db, slug: str, sanitized_html: str | None, thumbnail_key: str | None = None
) -> None:
    await (
        db.prepare("UPDATE newsletters SET sanitized_html = ?, thumbnail_key = ? WHERE slug = ?")
        .bind(sanitized_html, thumbnail_key, slug)
        .run()
    )


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


async def delete_newsletter(db, slug: str, deleted_by: str) -> bool:
    """Soft-delete: marks the row instead of removing it (nothing, including its
    images, is actually erased) so it can be reviewed/restored from /deleted."""
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare(
            "UPDATE newsletters SET deleted_at = ?, deleted_by = ? WHERE slug = ? AND deleted_at IS NULL"
        )
        .bind(now, deleted_by, slug)
        .run()
    )
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
        "FROM newsletters WHERE from_email IS NOT NULL AND quarantined_at IS NULL AND deleted_at IS NULL "
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


async def count_newsletters(db, *, sender: str | None = None) -> int:
    where = "WHERE quarantined_at IS NULL AND deleted_at IS NULL"
    params: list[str] = []
    if sender:
        where += " AND from_email = ?"
        params.append(sender)
    stmt = db.prepare(f"SELECT COUNT(*) AS n FROM newsletters {where}")
    if params:
        stmt = stmt.bind(*params)
    row = await stmt.first()
    return row["n"] if row else 0


async def list_newsletters(
    db,
    *,
    sender: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> list[NewsletterSummary]:
    where = "WHERE quarantined_at IS NULL AND deleted_at IS NULL"
    params: list[str] = []
    if sender:
        where += " AND from_email = ?"
        params.append(sender)
    direction = "ASC" if sort == "oldest" else "DESC"
    # SQLite treats a negative LIMIT as "no limit" -- callers pass 0 to mean "all".
    sql_limit = -1 if limit <= 0 else limit
    result = await (
        db.prepare(
            f"SELECT {_LIST_COLUMNS} FROM newsletters {where} "
            f"ORDER BY received_at {direction}, id {direction} LIMIT ? OFFSET ?"
        )
        .bind(*params, sql_limit, offset)
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
        thumbnail_key=row["thumbnail_key"],
        quarantined_at=row["quarantined_at"],
        deleted_at=row["deleted_at"],
        deleted_by=row["deleted_by"],
    )


def _row_to_newsletter(row) -> Newsletter:
    return Newsletter(
        **_row_to_summary(row).__dict__,
        sanitized_html=row["sanitized_html"],
        plain_text_fallback=row["plain_text_fallback"],
    )


@dataclass
class EmbedQuery:
    id: int
    token: str
    name: str
    sender_email: str | None
    result_limit: int
    sort: str
    created_by: str
    created_at: str
    show_thumbnails: bool = False


async def create_embed_query(
    db,
    *,
    token: str,
    name: str,
    sender_email: str | None,
    result_limit: int,
    sort: str,
    created_by: str,
    show_thumbnails: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare(
            "INSERT INTO embed_queries "
            "(token, name, sender_email, result_limit, sort, created_by, show_thumbnails, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(token, name, sender_email, result_limit, sort, created_by, int(show_thumbnails), now)
        .run()
    )


async def get_embed_query(db, token: str) -> EmbedQuery | None:
    row = await db.prepare("SELECT * FROM embed_queries WHERE token = ?").bind(token).first()
    return _row_to_embed_query(row) if row else None


async def list_embed_queries(db) -> list[EmbedQuery]:
    result = await db.prepare("SELECT * FROM embed_queries ORDER BY created_at DESC").all()
    return [_row_to_embed_query(row) for row in result.results]


async def update_embed_query(
    db,
    token: str,
    *,
    name: str,
    sender_email: str | None,
    result_limit: int,
    sort: str,
    show_thumbnails: bool = False,
) -> None:
    """Edits an existing embed's query in place -- same token, so any iframe already
    using it keeps working, just serving the newly saved filters going forward."""
    await (
        db.prepare(
            "UPDATE embed_queries SET name = ?, sender_email = ?, result_limit = ?, sort = ?, "
            "show_thumbnails = ? WHERE token = ?"
        )
        .bind(name, sender_email, result_limit, sort, int(show_thumbnails), token)
        .run()
    )


async def delete_embed_query(db, token: str) -> None:
    await db.prepare("DELETE FROM embed_queries WHERE token = ?").bind(token).run()


def _row_to_embed_query(row) -> EmbedQuery:
    return EmbedQuery(
        id=row["id"],
        token=row["token"],
        name=row["name"],
        sender_email=row["sender_email"],
        result_limit=row["result_limit"],
        sort=row["sort"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        show_thumbnails=bool(row["show_thumbnails"]),
    )


async def get_resolved_links(db, tracked_urls: list[str]) -> dict[str, str]:
    """Previously-resolved tracked URLs, so repeat resolution runs don't re-spend
    subrequest budget on links already solved in an earlier (possibly cut-short) run.
    Chunked (see _MAX_IN_CLAUSE_VALUES below) to stay under D1's bound-parameter limit
    regardless of how many tracked links a newsletter has."""
    if not tracked_urls:
        return {}
    resolved: dict[str, str] = {}
    for chunk in _chunked(tracked_urls, _MAX_IN_CLAUSE_VALUES):
        placeholders = ",".join("?" for _ in chunk)
        result = await (
            db.prepare(f"SELECT tracked_url, resolved_url FROM resolved_links WHERE tracked_url IN ({placeholders})")
            .bind(*chunk)
            .all()
        )
        resolved.update((r["tracked_url"], r["resolved_url"]) for r in result.results)
    return resolved


# D1 rejects a prepared statement once its total bound-parameter count gets too high
# ("D1_ERROR: too many SQL variables") -- confirmed in production once enough images
# needed mirroring in a single newsletter for save_mirrored_assets' multi-row INSERT to
# cross that line, which made the whole insert (and therefore the newsletter's progress)
# fail every single time, silently, with the newsletter's mirrored_assets rows always at
# zero. All of get_resolved_links/save_resolved_links/get_mirrored_assets/
# save_mirrored_assets below chunk their variable-length parameter lists to stay clear
# of that limit regardless of how many candidates one newsletter has.
_MAX_IN_CLAUSE_VALUES = 90  # + 1-2 fixed params per query, comfortably under D1's limit
_MAX_INSERT_ROWS = 15  # widest row here is 6 columns; 15 * 6 = 90, same margin


def _chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def save_resolved_links(db, mapping: dict[str, str]) -> None:
    """Batched as multi-row inserts (not one write per link) to keep this cheap against
    the same per-invocation subrequest budget the resolving itself is limited by, chunked
    to stay under D1's bound-parameter limit regardless of how many links there are."""
    if not mapping:
        return
    now = datetime.now(timezone.utc).isoformat()
    items = list(mapping.items())
    for chunk in _chunked(items, _MAX_INSERT_ROWS):
        values_sql = ", ".join("(?, ?, ?)" for _ in chunk)
        params: list[str] = []
        for tracked_url, resolved_url in chunk:
            params.extend([tracked_url, resolved_url, now])
        await (
            db.prepare(
                f"INSERT INTO resolved_links (tracked_url, resolved_url, resolved_at) VALUES {values_sql} "
                "ON CONFLICT(tracked_url) DO UPDATE SET resolved_url = excluded.resolved_url, resolved_at = excluded.resolved_at"
            )
            .bind(*params)
            .run()
        )


async def get_mirrored_assets(
    db, newsletter_slug: str, source_urls: list[str]
) -> dict[str, tuple[str, int | None]]:
    """Previously-mirrored (source_url -> (asset_key, size_bytes)) for this newsletter,
    so a repeat ingest/Reprocess run skips fetching anything an earlier run already
    mirrored -- same "make repeat runs incremental" idea as get_resolved_links above.
    size_bytes is also how the thumbnail (the largest mirrored image) gets picked
    without re-fetching anything just to compare sizes. Chunked (see
    _MAX_IN_CLAUSE_VALUES below) to stay under D1's bound-parameter limit regardless of
    how many images a newsletter has."""
    if not source_urls:
        return {}
    found: dict[str, tuple[str, int | None]] = {}
    for chunk in _chunked(source_urls, _MAX_IN_CLAUSE_VALUES):
        placeholders = ",".join("?" for _ in chunk)
        result = await (
            db.prepare(
                f"SELECT source_url, asset_key, size_bytes FROM mirrored_assets "
                f"WHERE newsletter_slug = ? AND source_url IN ({placeholders})"
            )
            .bind(newsletter_slug, *chunk)
            .all()
        )
        found.update((r["source_url"], (r["asset_key"], r["size_bytes"])) for r in result.results)
    return found


async def save_mirrored_assets(
    db, newsletter_slug: str, records: list[tuple[str, str, str, int]]
) -> None:
    """`records` is (source_url, asset_key, content_type, size_bytes) tuples. Kept
    indefinitely as provenance -- which original address a mirrored image came from --
    not just a cache, so a newsletter's mirrored images can always be traced back or
    reverted later. Chunked into _MAX_INSERT_ROWS-row inserts: this is a 6-column table,
    so without chunking, a newsletter with as few as ~17 newly-mirrored images in one
    run already crosses D1's bound-parameter limit -- confirmed in production, where it
    silently failed the whole insert (and therefore the newsletter's progress) every
    single time, for every newsletter with enough images to trigger it."""
    if not records:
        return
    now = datetime.now(timezone.utc).isoformat()
    for chunk in _chunked(records, _MAX_INSERT_ROWS):
        values_sql = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in chunk)
        params: list[str] = []
        for source_url, asset_key, content_type, size_bytes in chunk:
            params.extend([newsletter_slug, source_url, asset_key, content_type, size_bytes, now])
        await (
            db.prepare(
                f"INSERT INTO mirrored_assets "
                f"(newsletter_slug, source_url, asset_key, content_type, size_bytes, mirrored_at) VALUES {values_sql} "
                "ON CONFLICT(newsletter_slug, source_url) DO UPDATE SET "
                "asset_key = excluded.asset_key, content_type = excluded.content_type, "
                "size_bytes = excluded.size_bytes, mirrored_at = excluded.mirrored_at"
            )
            .bind(*params)
            .run()
        )


async def list_slugs_needing_backfill(db, limit: int) -> list[tuple[int, str]]:
    """(id, slug) for newsletters not yet successfully backfilled, never-attempted ones
    first and then previously-failed ones ordered by how many times they've failed --
    so a newsletter that keeps crashing its own request sinks behind fresh ones instead
    of blocking every batch forever, while still eventually coming back around to it."""
    result = await (
        db.prepare(
            "SELECT id, slug FROM newsletters WHERE backfilled_at IS NULL "
            "ORDER BY backfill_attempts ASC, id ASC LIMIT ?"
        )
        .bind(limit)
        .all()
    )
    return [(r["id"], r["slug"]) for r in result.results]


async def mark_backfill_attempt(db, slug: str) -> None:
    """Recorded *before* the risky reprocess work starts, specifically because a Workers
    CPU-time-limit termination kills the isolate outright and can't be caught in Python
    code -- this write is what still leaves a trace of a crashed attempt for the next
    batch to see and deprioritize."""
    await db.prepare("UPDATE newsletters SET backfill_attempts = backfill_attempts + 1 WHERE slug = ?").bind(
        slug
    ).run()


async def mark_backfill_complete(db, slug: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.prepare("UPDATE newsletters SET backfilled_at = ? WHERE slug = ?").bind(now, slug).run()


async def count_backfill_status(db) -> tuple[int, int, int]:
    """(total, fully_backfilled, failing_at_least_once) for the /permissions progress
    display -- "failing" means attempted but not yet successfully completed."""
    row = await db.prepare(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN backfilled_at IS NOT NULL THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN backfilled_at IS NULL AND backfill_attempts > 0 THEN 1 ELSE 0 END) AS failing "
        "FROM newsletters"
    ).first()
    return row["total"] or 0, row["done"] or 0, row["failing"] or 0


@dataclass
class SenderCard:
    from_email: str
    name: str
    count: int
    latest_received_at: str | None
    latest_slug: str
    latest_thumbnail_key: str | None


async def list_sender_cards(db) -> list[SenderCard]:
    """One row per sender: their newsletter count and their single latest newsletter
    (by received_at, falling back to id to break ties), for the homepage card grid.
    A plain GROUP BY MAX(received_at) doesn't reliably give you the *other* columns
    (slug, thumbnail_key) from that specific row, so this uses a window function
    instead -- deterministic, single query, one row per sender."""
    result = await db.prepare(
        """
        SELECT from_email, from_address, slug, thumbnail_key, received_at, created_at, sender_count
        FROM (
            SELECT from_email, from_address, slug, thumbnail_key, received_at, created_at,
                   COUNT(*) OVER (PARTITION BY from_email) AS sender_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY from_email ORDER BY received_at DESC, id DESC
                   ) AS rn
            FROM newsletters
            WHERE from_email IS NOT NULL AND quarantined_at IS NULL AND deleted_at IS NULL
        )
        WHERE rn = 1
        ORDER BY received_at DESC
        """
    ).all()
    return [
        SenderCard(
            from_email=r["from_email"],
            name=parseaddr(r["from_address"])[0] or r["from_email"],
            count=r["sender_count"],
            latest_received_at=r["received_at"] or r["created_at"],
            latest_slug=r["slug"],
            latest_thumbnail_key=r["thumbnail_key"],
        )
        for r in result.results
    ]


async def list_quarantined(db) -> list[NewsletterSummary]:
    result = await db.prepare(
        f"SELECT {_LIST_COLUMNS} FROM newsletters "
        "WHERE quarantined_at IS NOT NULL AND deleted_at IS NULL ORDER BY quarantined_at DESC"
    ).all()
    return [_row_to_summary(row) for row in result.results]


async def release_from_quarantine(db, slug: str) -> None:
    await db.prepare("UPDATE newsletters SET quarantined_at = NULL WHERE slug = ?").bind(slug).run()


async def release_all_from_sender(db, from_email: str) -> None:
    await (
        db.prepare("UPDATE newsletters SET quarantined_at = NULL WHERE from_email = ? AND quarantined_at IS NOT NULL")
        .bind(from_email)
        .run()
    )


async def list_deleted(db) -> list[NewsletterSummary]:
    result = await db.prepare(
        f"SELECT {_LIST_COLUMNS} FROM newsletters WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).all()
    return [_row_to_summary(row) for row in result.results]


async def restore_newsletter(db, slug: str) -> None:
    await (
        db.prepare("UPDATE newsletters SET deleted_at = NULL, deleted_by = NULL WHERE slug = ?")
        .bind(slug)
        .run()
    )


@dataclass
class AllowlistEntry:
    id: int
    email: str
    created_at: str


async def list_allowlist(db) -> list[AllowlistEntry]:
    result = await db.prepare("SELECT id, email, created_at FROM sender_allowlist ORDER BY email").all()
    return [
        AllowlistEntry(id=r["id"], email=r["email"], created_at=r["created_at"]) for r in result.results
    ]


async def is_sender_allowlisted(db, email: str) -> bool:
    row = await db.prepare("SELECT 1 AS present FROM sender_allowlist WHERE email = ?").bind(email).first()
    return row is not None


async def add_to_allowlist(db, email: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await (
        db.prepare("INSERT INTO sender_allowlist (email, created_at) VALUES (?, ?) ON CONFLICT(email) DO NOTHING")
        .bind(email, now)
        .run()
    )


async def remove_from_allowlist(db, entry_id: int) -> None:
    await db.prepare("DELETE FROM sender_allowlist WHERE id = ?").bind(entry_id).run()
