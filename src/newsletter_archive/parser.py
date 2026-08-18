"""Turn a raw MIME email (bytes) into structured data: headers, HTML/text body, inline images.

Deliberately knows nothing about where the bytes came from (file, IMAP fetch, webhook
POST body) -- that's the whole point of the ingest() seam in ingest.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime


@dataclass
class InlineImage:
    content_id: str
    content_type: str
    data: bytes


@dataclass
class ParsedEmail:
    message_id: str | None
    from_address: str
    subject: str
    date: datetime | None
    list_unsubscribe: str | None
    html_body: str | None
    text_body: str | None
    inline_images: list[InlineImage] = field(default_factory=list)


def parse_email(raw: bytes) -> ParsedEmail:
    msg = message_from_bytes(raw, policy=policy.default)

    date_header = msg.get("Date")
    date = None
    if date_header:
        try:
            date = parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            date = None

    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))

    inline_images: list[InlineImage] = []
    for part in msg.iter_attachments():
        content_id = part.get("Content-ID")
        if not content_id:
            continue
        content_type = part.get_content_type()
        if not content_type.startswith("image/"):
            continue
        data = part.get_content()
        if isinstance(data, str):
            data = data.encode("utf-8")
        inline_images.append(
            InlineImage(
                content_id=str(content_id).strip("<>"),
                content_type=content_type,
                data=data,
            )
        )

    return ParsedEmail(
        message_id=msg.get("Message-ID"),
        from_address=str(msg.get("From", "")),
        subject=str(msg.get("Subject", "(no subject)")),
        date=date,
        list_unsubscribe=msg.get("List-Unsubscribe"),
        html_body=html_part.get_content() if html_part is not None else None,
        text_body=text_part.get_content() if text_part is not None else None,
        inline_images=inline_images,
    )
