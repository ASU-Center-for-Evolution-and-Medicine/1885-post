"""Permalink slug generation.

Format: {YYYY-MM-DD}-{subject-slugified}-{short-hash}. The hash (derived from the
Message-ID, falling back to subject+date) guarantees uniqueness even when two
newsletters share a subject and date, and keeps slugs stable across re-ingestion.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    text = _NON_ALNUM.sub("-", text.lower().strip()).strip("-")
    return text[:80] or "newsletter"


def make_slug(subject: str, date: datetime | None, message_id: str | None) -> str:
    date_part = (date or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    subject_part = slugify(subject)
    seed = message_id or f"{subject}|{date_part}"
    short_hash = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"{date_part}-{subject_part}-{short_hash}"
