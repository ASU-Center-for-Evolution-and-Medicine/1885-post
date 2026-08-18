"""Targeted HTML rewrites applied to an archived newsletter body.

v1 scope is intentionally narrow: neutralize unsubscribe / manage-preferences links so a
public archive page can't be used to unsubscribe someone or leak per-subscriber tracking
tokens. Every other link and all other content is left exactly as it was. The pattern
lists below are meant to grow over time without needing a redesign.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_TEXT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"unsubscribe",
        r"manage.*(preferences|subscription)",
        r"update.*preferences",
        r"opt[- ]?out",
        r"email preferences",
    )
]

_HREF_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"unsubscribe",
        r"opt-?out",
        r"manage-preferences",
        r"list-manage\.com",
        r"preferences",
    )
]


def _is_unsubscribe_link(anchor) -> bool:
    text = anchor.get_text(separator=" ", strip=True)
    href = anchor.get("href", "")
    if text and any(pattern.search(text) for pattern in _TEXT_PATTERNS):
        return True
    if href and any(pattern.search(href) for pattern in _HREF_PATTERNS):
        return True
    return False


def neutralize_unsubscribe_links(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        if _is_unsubscribe_link(anchor):
            anchor["href"] = "#"
    return str(soup)


def rewrite_inline_image_sources(html: str, url_by_content_id: dict[str, str]) -> str:
    """Point <img src="cid:..."> references at the URLs we now serve those images from."""
    if not url_by_content_id:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("cid:") and src[len("cid:") :] in url_by_content_id:
            img["src"] = url_by_content_id[src[len("cid:") :]]
    return str(soup)
