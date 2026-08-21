from pathlib import Path

from bs4 import BeautifulSoup

from newsletter_archive.parser import parse_email
from newsletter_archive.sanitizer import (
    find_trackable_links,
    neutralize_unsubscribe_links,
    rewrite_tracked_links,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _links_by_text(html: str) -> dict[str, str | None]:
    """href per anchor (by visible text), or None if the anchor has no href at all --
    neutralize_unsubscribe_links removes the attribute rather than setting href="#"."""
    soup = BeautifulSoup(html, "html.parser")
    return {a.get_text(strip=True): a.get("href") for a in soup.find_all("a")}


def test_neutralizes_unsubscribe_and_preferences_links_only():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = neutralize_unsubscribe_links(html)
    links_by_text = _links_by_text(sanitized)

    assert links_by_text["Unsubscribe"] is None
    assert links_by_text["Manage your preferences"] is None
    # content links must be untouched
    assert links_by_text["Read about our new product launch"].startswith(
        "https://acme-newsletter.com/issue-42/product-launch"
    )
    assert links_by_text["Customer spotlight: how Acme helped a customer ship faster"].startswith(
        "https://acme-newsletter.com/issue-42/customer-story"
    )


def test_does_not_touch_view_in_browser_or_content_links():
    raw = (FIXTURES / "substack_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = neutralize_unsubscribe_links(html)
    links_by_text = _links_by_text(sanitized)

    assert links_by_text["View this post in your browser"].startswith("https://")
    assert links_by_text["Leave a comment"].startswith("https://")
    assert links_by_text["Opt out of these emails"] is None


def test_all_visible_text_content_is_preserved():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = neutralize_unsubscribe_links(html)

    original_text = BeautifulSoup(html, "html.parser").get_text()
    sanitized_text = BeautifulSoup(sanitized, "html.parser").get_text()
    assert original_text == sanitized_text


def test_find_trackable_links_matches_configured_domain_only():
    html = (
        '<a href="https://click.reply.asu.edu/?qs=abc123">Read more</a>'
        '<a href="https://example.com/direct-link">Direct link</a>'
    )
    assert find_trackable_links(html) == {"https://click.reply.asu.edu/?qs=abc123"}


def test_rewrite_tracked_links_only_touches_resolved_hrefs():
    html = (
        '<a href="https://click.reply.asu.edu/?qs=abc123">Read more</a>'
        '<a href="https://example.com/direct-link">Direct link</a>'
    )
    resolved = {"https://click.reply.asu.edu/?qs=abc123": "https://evmed.asu.edu/real-article"}
    rewritten = rewrite_tracked_links(html, resolved)
    links_by_text = _links_by_text(rewritten)

    assert links_by_text["Read more"] == "https://evmed.asu.edu/real-article"
    assert links_by_text["Direct link"] == "https://example.com/direct-link"


def test_resolved_smc_unsubscribe_link_gets_neutralized():
    # The whole point of resolving before classifying: a tracked href reveals nothing
    # about the destination, so neutralize_unsubscribe_links can only catch SMC's real
    # unsubscribe/preference-center link once it's been resolved.
    html = (
        '<a href="https://click.reply.asu.edu/?qs=xyz">Update Profile</a>'
        '<a href="https://click.reply.asu.edu/?qs=abc">Read the newsletter</a>'
    )
    resolved = {
        "https://click.reply.asu.edu/?qs=xyz": "https://view.email.asu.edu/subscriptioncenter.aspx?qs=xyz",
        "https://click.reply.asu.edu/?qs=abc": "https://evmed.asu.edu/real-article",
    }
    sanitized = neutralize_unsubscribe_links(rewrite_tracked_links(html, resolved))
    links_by_text = _links_by_text(sanitized)

    assert links_by_text["Update Profile"] is None
    assert links_by_text["Read the newsletter"] == "https://evmed.asu.edu/real-article"


def test_smc_profile_center_link_is_neutralized_even_unresolved():
    # Real-world case verified against live archived newsletters: "Update Profile"
    # points at click.reply.asu.edu/profile_center.aspx directly (SMC serves this page
    # on the tracking domain itself -- no redirect happens), so this must be caught by
    # href pattern alone even if resolution never runs or fails.
    html = (
        '<a href="https://click.reply.asu.edu/profile_center.aspx?qs=ABB7abc123">Update Profile</a>'
        '<a href="https://click.reply.asu.edu/?qs=abc">Read the newsletter</a>'
    )
    sanitized = neutralize_unsubscribe_links(html)
    links_by_text = _links_by_text(sanitized)

    assert links_by_text["Update Profile"] is None
    # untouched -- still the tracked link, since this test doesn't resolve it
    assert links_by_text["Read the newsletter"] == "https://click.reply.asu.edu/?qs=abc"
