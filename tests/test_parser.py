from pathlib import Path

from newsletter_archive.parser import parse_email

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_headers_and_html_body():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    parsed = parse_email(raw)

    assert parsed.subject == "Acme Weekly - Issue 42"
    assert parsed.from_address == "Acme Weekly <news@acme-newsletter.com>"
    assert parsed.message_id == "<mc-issue-42@list-manage.com>"
    assert parsed.date is not None
    assert parsed.date.year == 2026
    assert "<h1>Acme Weekly - Issue 42</h1>" in parsed.html_body
    assert "product-launch" in parsed.html_body


def test_falls_back_to_plain_text_when_no_html():
    raw = (FIXTURES / "plaintext_only.eml").read_bytes()
    parsed = parse_email(raw)

    assert parsed.html_body is None
    assert "This week in small ops" in parsed.text_body


def test_no_inline_images_when_none_present():
    raw = (FIXTURES / "mailchimp_style.eml").read_bytes()
    parsed = parse_email(raw)

    assert parsed.inline_images == []
