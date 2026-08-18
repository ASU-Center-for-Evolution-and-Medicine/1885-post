"""Cloudflare Access identity for multi-organization authorization.

Trust boundary: identity comes from Cloudflare Access's own /cdn-cgi/access/get-identity
endpoint via an internal subrequest that forwards the incoming request's Cookie header --
not from client-supplied headers. A client can't forge this: /cdn-cgi/* paths are
intercepted by Cloudflare's edge before they'd ever reach this Worker, and the endpoint's
response is resolved from the real CF_Authorization session, not from anything we're
trusting off the wire. If the request didn't come through an Access-protected hostname
(no valid session), the subrequest simply won't authenticate, and access fails closed --
list/permalink routes treat "no identity" as "no allowed addresses" in web/app.py.
"""

from __future__ import annotations

from workers import fetch as workers_fetch


async def get_identity(request) -> dict | None:
    cookie = request.headers.get("cookie")
    if not cookie:
        return None

    identity_url = f"{str(request.base_url).rstrip('/')}/cdn-cgi/access/get-identity"
    try:
        resp = await workers_fetch(identity_url, headers={"Cookie": cookie})
    except OSError:
        return None
    if not resp.ok:
        return None
    try:
        identity = await resp.json()
    except Exception:
        return None
    return identity if isinstance(identity, dict) else None


def identity_email(identity: dict | None) -> str | None:
    email = (identity or {}).get("email")
    return email.lower() if email else None


def display_name(identity: dict | None) -> str | None:
    """Name -> username -> email, whichever is present first, per product requirement."""
    if not identity:
        return None
    return identity.get("name") or identity.get("username") or identity.get("email")
