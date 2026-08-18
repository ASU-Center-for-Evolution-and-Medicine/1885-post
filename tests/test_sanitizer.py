from pathlib import Path

from bs4 import BeautifulSoup

from newsletter_archive.parser import parse_email
from newsletter_archive.sanitizer import neutralize_unsubscribe_links

FIXTURES = Path(__file__).parent / "fixtures"


def test_neutralizes_unsubscribe_and_preferences_links_only():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = neutralize_unsubscribe_links(html)
    soup = BeautifulSoup(sanitized, "html.parser")

    links_by_text = {a.get_text(strip=True): a["href"] for a in soup.find_all("a", href=True)}

    assert links_by_text["Unsubscribe"] == "#"
    assert links_by_text["Manage your preferences"] == "#"
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
    soup = BeautifulSoup(sanitized, "html.parser")

    links_by_text = {a.get_text(strip=True): a["href"] for a in soup.find_all("a", href=True)}

    assert links_by_text["View this post in your browser"].startswith("https://")
    assert links_by_text["Leave a comment"].startswith("https://")
    assert links_by_text["Opt out of these emails"] == "#"


def test_all_visible_text_content_is_preserved():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = neutralize_unsubscribe_links(html)

    original_text = BeautifulSoup(html, "html.parser").get_text()
    sanitized_text = BeautifulSoup(sanitized, "html.parser").get_text()
    assert original_text == sanitized_text
