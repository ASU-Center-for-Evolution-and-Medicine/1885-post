"""SQLite persistence for archived newsletters and their inline images.

Deliberately a thin wrapper around stdlib sqlite3 rather than an ORM -- the query
shapes here (insert, get-by-slug, filter-by-date/sender/subject) don't need one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "newsletter_archive.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    from_address TEXT NOT NULL,
    from_email TEXT,
    to_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    received_at TEXT,
    slug TEXT NOT NULL UNIQUE,
    raw_eml BLOB NOT NULL,
    sanitized_html TEXT,
    plain_text_fallback TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_newsletters_received_at ON newsletters(received_at);
CREATE INDEX IF NOT EXISTS idx_newsletters_from_address ON newsletters(from_address);
CREATE INDEX IF NOT EXISTS idx_newsletters_to_address ON newsletters(to_address);

CREATE TABLE IF NOT EXISTS newsletter_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id INTEGER NOT NULL REFERENCES newsletters(id),
    content_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    UNIQUE(newsletter_id, content_id)
);
"""

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


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def insert_newsletter(
    conn: sqlite3.Connection,
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
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO newsletters
            (message_id, from_address, from_email, to_address, subject, received_at, slug,
             raw_eml, sanitized_html, plain_text_fallback, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
        ),
    )
    conn.commit()
    return cur.lastrowid


def insert_image(
    conn: sqlite3.Connection,
    *,
    newsletter_id: int,
    content_id: str,
    content_type: str,
    data: bytes,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO newsletter_images (newsletter_id, content_id, content_type, data)
        VALUES (?, ?, ?, ?)
        """,
        (newsletter_id, content_id, content_type, data),
    )
    conn.commit()


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Newsletter | None:
    row = conn.execute(
        f"SELECT {_DETAIL_COLUMNS} FROM newsletters WHERE slug = ?", (slug,)
    ).fetchone()
    return _row_to_newsletter(row) if row else None


def get_image(conn: sqlite3.Connection, slug: str, content_id: str) -> tuple[str, bytes] | None:
    row = conn.execute(
        """
        SELECT ni.content_type, ni.data
        FROM newsletter_images ni
        JOIN newsletters n ON n.id = ni.newsletter_id
        WHERE n.slug = ? AND ni.content_id = ?
        """,
        (slug, content_id),
    ).fetchone()
    return (row["content_type"], row["data"]) if row else None


def list_newsletters(
    conn: sqlite3.Connection,
    *,
    date: str | None = None,
    from_address: str | None = None,
    subject: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[NewsletterSummary]:
    clauses = []
    params: list[str] = []
    if date:
        clauses.append("received_at LIKE ?")
        params.append(f"{date}%")
    if from_address:
        clauses.append("from_address LIKE ?")
        params.append(f"%{from_address}%")
    if subject:
        clauses.append("subject LIKE ?")
        params.append(f"%{subject}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_LIST_COLUMNS} FROM newsletters {where} "
        "ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_summary(row) for row in rows]


def _row_to_summary(row: sqlite3.Row) -> NewsletterSummary:
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


def _row_to_newsletter(row: sqlite3.Row) -> Newsletter:
    return Newsletter(
        **_row_to_summary(row).__dict__,
        sanitized_html=row["sanitized_html"],
        plain_text_fallback=row["plain_text_fallback"],
    )
