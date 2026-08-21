from pathlib import Path

from newsletter_archive import storage
from newsletter_archive.ingest import ingest

FIXTURES = Path(__file__).parent / "fixtures"


def test_ingest_stores_and_returns_newsletter():
    conn = storage.connect(":memory:")
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()

    newsletter = ingest(raw, "newsletter-archive@example.com", conn=conn)

    assert newsletter.subject == "Acme Weekly - Issue 42"
    assert newsletter.to_address == "newsletter-archive@example.com"
    assert newsletter.slug.startswith("2026-08-01-acme-weekly-issue-42-")
    assert "list-manage.com/unsubscribe" not in newsletter.sanitized_html
    assert '<a>Unsubscribe</a>' in newsletter.sanitized_html  # href removed, text preserved

    fetched = storage.get_by_slug(conn, newsletter.slug)
    assert fetched is not None
    assert fetched.subject == newsletter.subject


def test_ingest_plaintext_only_has_no_html_body():
    conn = storage.connect(":memory:")
    raw = (FIXTURES / "plaintext_only.eml").read_bytes()

    newsletter = ingest(raw, "newsletter-archive@example.com", conn=conn)

    assert newsletter.sanitized_html is None
    assert "This week in small ops" in newsletter.plain_text_fallback


def test_ingest_is_idempotent_for_duplicate_message_id():
    conn = storage.connect(":memory:")
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()

    first = ingest(raw, "newsletter-archive@example.com", conn=conn)
    second = ingest(raw, "newsletter-archive@example.com", conn=conn)

    assert first.slug == second.slug
    assert len(storage.list_newsletters(conn)) == 1


def test_ingest_is_queryable_by_from_address_and_subject():
    conn = storage.connect(":memory:")
    for name in ("mailchimp_style.eml", "substack_style.eml", "plaintext_only.eml"):
        ingest((FIXTURES / name).read_bytes(), "newsletter-archive@example.com", conn=conn)

    by_subject = storage.list_newsletters(conn, subject="shipping slower")
    assert len(by_subject) == 1
    assert by_subject[0].subject == "Notes on shipping slower to go faster"

    by_sender = storage.list_newsletters(conn, from_address="acme-newsletter.com")
    assert len(by_sender) == 1

    by_date = storage.list_newsletters(conn, date="2026-08-05")
    assert len(by_date) == 1
