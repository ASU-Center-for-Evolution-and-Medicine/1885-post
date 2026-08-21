"""Targeted HTML rewrites applied to an archived newsletter body.

v1 scope is intentionally narrow: neutralize unsubscribe / manage-preferences links so a
public archive page can't be used to unsubscribe someone or leak per-subscriber tracking
tokens. Every other link and all other content is left exactly as it was. The pattern
lists below are meant to grow over time without needing a redesign.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

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
        # Salesforce Marketing Cloud preference-center/unsubscribe URL shapes.
        # profile_center.aspx confirmed against real archived newsletters (their
        # "Update Profile" footer link, served directly on the click.reply.asu.edu
        # tracking domain itself -- no further redirect, so this matches whether or not
        # link resolution succeeds). The rest are unconfirmed but harmless-if-wrong
        # coverage for other SMC installations/link shapes.
        r"profile_center\.aspx",
        r"subscriptioncenter",
        r"/asp/unsub",
        r"pub\.sfmc",
        r"exacttarget",
    )
]

# Click-tracking domains whose links expire and whose hrefs are opaque (reveal nothing
# about the real destination, which is why _HREF_PATTERNS above can't see through them
# unresolved). Extend this list the moment another org/ESP shows up.
TRACKED_LINK_DOMAINS = [
    re.compile(p, re.IGNORECASE)
    for p in (r"^click\.reply\.asu\.edu$",)
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
            # Removing the attribute (not setting href="#") makes the link genuinely
            # inert -- "#" is still a real navigation target, and clicking it inside the
            # sandboxed newsletter-body iframe visibly jumps/flashes as if the page were
            # reloading. An <a> with no href at all isn't a hyperlink; nothing happens on
            # click. (Newsletter content runs inside a sandbox without allow-scripts, so
            # an onclick-based approach wouldn't fire anyway -- this is also just simpler.)
            del anchor["href"]
    return str(soup)


def _is_tracked_link(href: str) -> bool:
    host = urlparse(href).hostname or ""
    return any(pattern.match(host) for pattern in TRACKED_LINK_DOMAINS)


def find_trackable_links(html: str) -> set[str]:
    """Unique hrefs pointing at a known click-tracking domain, worth resolving to their
    real (permanent, de-tracked) destination before they expire."""
    soup = BeautifulSoup(html, "html.parser")
    return {
        anchor["href"]
        for anchor in soup.find_all("a", href=True)
        if _is_tracked_link(anchor["href"])
    }


def rewrite_tracked_links(html: str, resolved: dict[str, str]) -> str:
    """Point tracked hrefs at their resolved destination. Anything not in `resolved`
    (resolution failed, or wasn't a tracked link to begin with) is left untouched."""
    if not resolved:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        if anchor["href"] in resolved:
            anchor["href"] = resolved[anchor["href"]]
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
