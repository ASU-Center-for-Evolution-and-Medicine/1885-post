from pathlib import Path

from bs4 import BeautifulSoup

from newsletter_archive.parser import parse_email
from newsletter_archive.sanitizer import (
    find_external_css_images,
    find_external_images,
    find_trackable_links,
    force_links_new_tab,
    neutralize_unsubscribe_links,
    rewrite_css_image_urls,
    rewrite_external_images,
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


def test_force_links_new_tab_normalizes_every_target():
    html = (
        '<a href="https://example.com/no-target">No target</a>'
        '<a href="https://example.com/self" target="_self">Self target</a>'
        '<a href="https://example.com/blank" target="_blank">Already blank</a>'
        '<a>No href at all</a>'
    )
    soup = BeautifulSoup(force_links_new_tab(html), "html.parser")
    anchors = {a.get_text(strip=True): a for a in soup.find_all("a")}

    for text in ("No target", "Self target", "Already blank"):
        assert anchors[text]["target"] == "_blank"
        # BeautifulSoup treats "rel" as a multi-valued attribute, so re-parsing the
        # serialized HTML gives a list back rather than the original space-joined string.
        assert anchors[text]["rel"] == ["noopener", "noreferrer"]

    assert anchors["No href at all"].get("target") is None


def test_force_links_new_tab_does_not_touch_neutralized_unsubscribe_links():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    html = parse_email(raw).html_body
    sanitized = force_links_new_tab(neutralize_unsubscribe_links(html))
    links_by_text = _links_by_text(sanitized)

    assert links_by_text["Unsubscribe"] is None
    assert links_by_text["Manage your preferences"] is None
    soup = BeautifulSoup(sanitized, "html.parser")
    content_link = next(a for a in soup.find_all("a") if a.get("href", "").startswith("https://acme-newsletter.com"))
    assert content_link["target"] == "_blank"


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


# --- external image mirroring -----------------------------------------------------
# Fixtures below are modeled on real patterns found in the live archive: a normal
# content image, a 1x1 open-tracking pixel (with and without an unresolved SMC merge
# tag), and a CSS-only "image carousel" template that sets backgrounds via <style>
# rather than <img> -- itself sometimes abused for the same kind of tracking pixel.


def test_find_external_images_excludes_tracking_pixels_and_merge_tags():
    html = (
        '<img src="https://image.reply.asu.edu/lib/abc/photo.jpg" alt="Real photo">'
        '<img src="https://click.reply.asu.edu/open.aspx?d=1" width="1" height="1" alt="">'
        '<img src="https://hzqaoo6b.emltrk.com/v2/hzqaoo6b?i=%%subscriberid%%" width="1" height="1">'
        '<img src="cid:logo123" alt="inline logo">'
    )
    assert find_external_images(html) == {"https://image.reply.asu.edu/lib/abc/photo.jpg"}


def test_rewrite_external_images_only_touches_mirrored_urls():
    html = (
        '<img src="https://image.reply.asu.edu/lib/abc/photo.jpg">'
        '<img src="https://click.reply.asu.edu/open.aspx?d=1" width="1" height="1">'
    )
    original_url = "https://image.reply.asu.edu/lib/abc/photo.jpg"
    mirrored = {original_url: "/n/some-slug/assets/deadbeef"}
    rewritten = rewrite_external_images(html, mirrored)
    soup = BeautifulSoup(rewritten, "html.parser")
    imgs = soup.find_all("img")
    srcs = [img["src"] for img in imgs]

    assert "/n/some-slug/assets/deadbeef" in srcs
    assert "https://click.reply.asu.edu/open.aspx?d=1" in srcs  # tracking pixel untouched

    mirrored_img = next(img for img in imgs if img["src"] == "/n/some-slug/assets/deadbeef")
    assert mirrored_img["data-original-src"] == original_url
    assert original_url in mirrored_img["title"]

    untouched_img = next(img for img in imgs if img["src"] == "https://click.reply.asu.edu/open.aspx?d=1")
    assert untouched_img.get("data-original-src") is None
    assert untouched_img.get("title") is None


def test_rewrite_external_images_preserves_existing_title():
    html = '<img src="https://image.reply.asu.edu/lib/abc/photo.jpg" title="Team photo">'
    original_url = "https://image.reply.asu.edu/lib/abc/photo.jpg"
    rewritten = rewrite_external_images(html, {original_url: "/n/some-slug/assets/deadbeef"})
    soup = BeautifulSoup(rewritten, "html.parser")
    img = soup.find("img")

    assert img["title"].startswith("Team photo")
    assert original_url in img["title"]


def test_find_external_css_images_excludes_merge_tagged_tracking_pixel():
    html = (
        "<style>"
        ".mc-carousel-id-1 .ie-img1 span {"
        " background-image: url(https://image.reply.asu.edu/lib/fe37/photo.jpg); }"
        "table.moz-email-headers-table {"
        " background-image:url('https://xqouujdu.emltrk.com/v2/xqouujdu?i=%%subscriberid%%')"
        " }"
        "</style>"
    )
    assert find_external_css_images(html) == {"https://image.reply.asu.edu/lib/fe37/photo.jpg"}


def test_rewrite_css_image_urls_only_touches_mirrored_urls():
    html = (
        "<style>.hero { background-image: url(https://image.reply.asu.edu/lib/fe37/photo.jpg); }"
        " .tracker { background-image:url('https://xqouujdu.emltrk.com/v2/xqouujdu?i=%%subscriberid%%') }"
        "</style>"
    )
    mirrored = {"https://image.reply.asu.edu/lib/fe37/photo.jpg": "/n/some-slug/assets/cafef00d"}
    rewritten = rewrite_css_image_urls(html, mirrored)

    assert "url(/n/some-slug/assets/cafef00d)" in rewritten
    assert "xqouujdu.emltrk.com" in rewritten  # tracking pixel background left untouched


def test_rewrite_css_image_urls_is_noop_without_matches():
    html = "<style>.hero { background-image: url(https://example.com/a.jpg); }</style>"
    assert rewrite_css_image_urls(html, {}) == html
