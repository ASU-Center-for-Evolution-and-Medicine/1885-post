import asyncio
import base64
import sys
from types import ModuleType


workers_stub = ModuleType("workers")


async def _unused_workers_fetch(*args, **kwargs):
    raise AssertionError("Cloudflare fetch should not run in branding tests")


workers_stub.fetch = _unused_workers_fetch
sys.modules.setdefault("workers", workers_stub)

from newsletter_archive.web import app as web_app
from newsletter_archive.web.assets import APP_MARK_PNG_BASE64


def test_app_uses_the_1885_post_brand() -> None:
    assert web_app.app.title == "The 1885 Post"

    templates = "\n".join(web_app._TEMPLATES.values())
    assert "The 1885 Post" in templates
    assert "An Arizona State University newsletter archive" in templates
    assert "Newsletter Archive" not in templates
    assert "Center for Evolution and Medicine" in templates


def test_app_mark_is_a_transparent_png() -> None:
    image = base64.b64decode(APP_MARK_PNG_BASE64)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"tRNS" in image


def test_app_mark_route_serves_png() -> None:
    response = asyncio.run(web_app.app_mark_png())

    assert response.media_type == "image/png"
    assert response.body.startswith(b"\x89PNG\r\n\x1a\n")
