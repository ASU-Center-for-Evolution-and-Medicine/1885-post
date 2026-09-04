"""FastAPI publisher, adapted to run inside a Cloudflare Python Worker.

Runs via the Workers-provided ASGI adapter (see worker_entry.py's `fetch` handler) and
reads/writes D1 through `request.scope["env"].DB`. Templates are in-memory Jinja2
strings (via DictLoader) rather than `Jinja2Templates(directory=...)` -- Cloudflare's
own FastAPI example (python-workers-examples/03-fastapi) renders Jinja2 from an
in-memory `Environment` rather than loading template files, so this follows the same,
verified-working pattern instead of gambling on file-based template/static loading
under the bundled Pyodide filesystem.
"""

from __future__ import annotations

import base64
import math
import secrets
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import parse_qs

import jinja2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import access
from .. import storage_d1 as storage
from .assets import APP_MARK_PNG_BASE64, FAVICON_ICO_BASE64, LOGO_PNG_BASE64

app = FastAPI(title="The 1885 Post")

def _env_var(request: Request, name: str) -> str | None:
    """Cloudflare Worker vars/secrets arrive on the ASGI scope's env object (like
    SUPER_ADMIN_EMAILS, read the same way in _is_super_admin below), not os.environ --
    this Python Worker's env is never actually populated into the process environment,
    so os.environ.get(...) silently returns None for a var/secret that IS set. Confirmed
    the hard way: NEWSLETTER_ARCHIVE_INGEST_TOKEN and BACKFILL_MAINTENANCE_TOKEN both
    read as None via os.environ even after being set, making their gates permanently
    inert regardless of configuration."""
    return getattr(request.scope["env"], name, None) or None

_PAGE_SIZE = 25


def _pagination_items(page: int, total_pages: int, radius: int = 2) -> list[int | None]:
    """Page numbers to render as buttons, with None marking an ellipsis gap. Always
    includes page 1 and the last page so far-away jumps stay reachable as the archive
    grows well past a handful of pages."""
    if total_pages <= 1:
        return [1]
    pages = {1, total_pages}
    for p in range(max(1, page - radius), min(total_pages, page + radius) + 1):
        pages.add(p)
    items: list[int | None] = []
    prev = None
    for p in sorted(pages):
        if prev is not None and p - prev > 1:
            items.append(None)
        items.append(p)
        prev = p
    return items


_CSS = """
:root {
  --asu-maroon: #8c1d40;
  --asu-gold: #ffc627;
  --asu-black: #000000;
  --asu-gray: #747474;
  --asu-sand: #f5ede0;
  --background: #f2f2f5;
  --card-bg: #ffffff;
  --text-primary: #1a1a1a;
  --text-muted: #4a4a58;
  --border: rgba(140, 29, 64, 0.15);
  font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--background);
  color: var(--text-primary);
}

a { color: var(--asu-maroon); text-decoration: none; }

.site-header {
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
}
.site-header-inner {
  max-width: 900px; margin: 0 auto; padding: 0.9rem 2rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
}
.site-header-inner.wide { max-width: 1150px; }
.site-brand {
  display: flex; align-items: center; gap: 0.75rem; min-width: 0; text-decoration: none;
}
.site-mark {
  display: block; width: 82px; height: 50px; object-fit: contain; flex: 0 0 auto;
}
.site-titles { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.site-title { color: var(--asu-maroon); font-weight: 700; font-size: 1.1rem; }
.site-subtitle { color: var(--text-muted); font-size: 0.75rem; max-width: 18rem; }
.header-right { display: flex; align-items: center; gap: 1rem; }
.site-nav { display: flex; align-items: center; gap: 1.5rem; }
.nav-link {
  color: var(--text-primary); font-size: 0.9rem; font-weight: 600; text-decoration: none;
  padding: 0.3rem 0; border-bottom: 2px solid transparent;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.nav-link:hover { color: var(--asu-maroon); border-bottom-color: var(--asu-maroon); }
.identity { text-align: right; font-size: 0.85rem; line-height: 1.35; padding-left: 1rem; border-left: 1px solid var(--border); }
.identity-name { color: var(--text-primary); font-weight: 600; }
.identity-email { color: var(--text-muted); }

main {
  max-width: 900px; margin: 1.5rem auto; padding: 2rem;
  background: var(--card-bg);
  border-radius: 20px;
  border: 1px solid var(--border);
  box-shadow: 0 25px 60px -30px rgba(140, 29, 64, 0.35);
}
main.wide { max-width: 1150px; }

main > h1 {
  margin-top: 0; margin-bottom: 1.5rem;
  color: var(--asu-black);
  background: var(--asu-gold);
  padding: 0.2rem 0.5rem;
  display: inline-block;
  font-size: 1.3rem;
}

.layout { display: flex; gap: 2rem; align-items: flex-start; }
.sidebar { width: 240px; flex-shrink: 0; }
.content { flex: 1; min-width: 0; }

.admin-sidebar {
  background: var(--asu-sand);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1.25rem;
  box-shadow:
    0 6px 16px -6px rgba(140, 29, 64, 0.4),
    inset 0 1px 0 rgba(255, 255, 255, 0.7),
    inset 0 -1px 0 rgba(140, 29, 64, 0.12);
}
.admin-sidebar .sidebar-section { margin-bottom: 0; }

.sidebar-section { margin-bottom: 1.75rem; }
.sidebar-section h2 {
  font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted); margin: 0 0 0.5rem;
}
.sidebar-link {
  display: flex; flex-direction: column; padding: 0.4rem 0.6rem; margin: 0 -0.6rem;
  border-radius: 6px; color: var(--text-primary); text-decoration: none; font-size: 0.9rem;
  overflow-wrap: anywhere;
}
.sidebar-link:hover { background: var(--asu-sand); }
.sidebar-link.active { background: var(--asu-maroon); color: #fff; font-weight: 600; }
.sidebar-link .count { color: var(--text-muted); font-size: 0.8rem; font-weight: 400; }
.sidebar-link.active .count { color: rgba(255, 255, 255, 0.75); }
.sidebar-link .sender-email { color: var(--text-muted); font-size: 0.78rem; font-weight: 400; }
.sidebar-link.active .sender-email { color: rgba(255, 255, 255, 0.75); }

@media (max-width: 720px) {
  .site-header-inner { padding: 0.75rem 1rem; align-items: flex-start; flex-wrap: wrap; }
  .site-brand { width: 100%; }
  .site-mark { width: 72px; height: 44px; }
  .site-subtitle { max-width: none; }
  .header-right { width: 100%; flex-wrap: wrap; gap: 0.5rem; }
  .identity { flex: 1 0 100%; margin-left: 0; text-align: left; overflow-wrap: anywhere; padding-left: 0; border-left: none; }
  .layout { flex-direction: column; }
  .sidebar { width: 100%; }
  .content { width: 100%; }
}

.newsletter-list { list-style: none; padding: 0; margin: 0; }
.newsletter-list li { padding: 0.9rem 0; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.9rem; }
.newsletter-list .li-text { min-width: 0; }
.newsletter-list a.subject { color: var(--text-primary); font-weight: 600; text-decoration: none; }
.newsletter-list a.subject:hover { color: var(--asu-maroon); }

.thumb-link { display: block; flex: 0 0 auto; }
.thumb { width: 56px; height: 56px; border-radius: 8px; object-fit: cover; flex: 0 0 auto; }
.thumb-placeholder {
  width: 56px; height: 56px; border-radius: 8px; flex: 0 0 auto;
  background: var(--asu-sand); color: var(--asu-maroon); font-weight: 700; font-size: 1.3rem;
  display: flex; align-items: center; justify-content: center; border: 1px solid var(--border);
}

.sender-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1.25rem; }
.sender-card {
  display: flex; flex-direction: column; text-decoration: none; color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 14px; overflow: hidden; background: var(--card-bg);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.sender-card:hover { transform: translateY(-2px); box-shadow: 0 15px 30px -20px rgba(140, 29, 64, 0.5); }
.sender-card-thumb-wrap { position: relative; }
.sender-card-thumb { width: 100%; height: 140px; object-fit: cover; display: block; }
img.sender-card-thumb { filter: brightness(0.65); }
.sender-card-thumb--placeholder {
  display: flex; align-items: center; justify-content: center;
  background: var(--asu-sand); color: var(--asu-maroon); font-weight: 700; font-size: 2.5rem;
}
.sender-card-badge {
  position: absolute; top: 0.5rem; right: 0.5rem;
  background: var(--asu-maroon); color: #fff;
  font-size: 0.75rem; font-weight: 700; line-height: 1;
  min-width: 1.6rem; height: 1.6rem; padding: 0 0.4rem;
  border-radius: 999px; display: flex; align-items: center; justify-content: center;
  border: 2px solid #fff; box-shadow: 0 3px 10px rgba(0,0,0,0.5);
}
.sender-card-body { padding: 0.9rem 1rem; }
.sender-card-name {
  display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
  font-weight: 700; color: var(--asu-maroon); margin-bottom: 0.25rem;
}
.sender-card-arrow {
  opacity: 0; transform: translateX(-4px);
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.sender-card:hover .sender-card-arrow { opacity: 1; transform: translateX(0); }
.sender-card-meta { font-size: 0.82rem; color: var(--text-muted); }

.view-all-card {
  flex-direction: column; align-items: center; justify-content: center; gap: 0.4rem;
  min-height: 210px; background: var(--asu-maroon); border: none; color: #fff;
}
.view-all-main { display: flex; align-items: center; gap: 0.6rem; font-weight: 800; font-size: 1.3rem; letter-spacing: 0.02em; }
.view-all-stats { font-size: 0.8rem; font-weight: 500; color: rgba(255, 255, 255, 0.8); }
.view-all-card:hover { background: var(--asu-black); }

.meta { color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem; }

.pagination { display: flex; align-items: center; gap: 0.4rem; margin-top: 1.5rem; flex-wrap: wrap; }
.page-btn {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 2.2rem; height: 2.2rem; padding: 0 0.7rem;
  border: 1px solid var(--asu-maroon); border-radius: 6px;
  color: var(--asu-maroon); font-weight: 600; font-size: 0.85rem; text-decoration: none;
}
a.page-btn:hover { background: var(--asu-maroon); color: #fff; text-decoration: none; }
.page-btn.active { background: var(--asu-maroon); color: #fff; }
.page-ellipsis { color: var(--text-muted); padding: 0 0.2rem; }

.delete-form { display: inline-block; margin: 0; padding: 0; }
.delete-btn {
  background: #fff; color: #c0392b; border: 1px solid #c0392b; border-radius: 6px;
  font-size: 0.8rem; font-weight: 600; padding: 0.3rem 0.7rem;
  cursor: pointer; font-family: inherit; transition: background 0.15s ease, color 0.15s ease;
}
.delete-btn:hover { background: #c0392b; color: #fff; }

.date-form { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.75rem; }
.date-form input {
  padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: 8px;
  background: rgba(255, 255, 255, 0.9); color: var(--text-primary); font-size: 0.9rem;
}
.date-form input:focus { outline: 2px solid var(--asu-gold); border-color: var(--asu-maroon); }
.date-form button, .secondary-btn {
  padding: 0.4rem 0.8rem; border: 1px solid var(--asu-maroon); border-radius: 6px;
  background: #fff; color: var(--asu-maroon); font-weight: 600; font-size: 0.85rem; cursor: pointer;
  font-family: inherit;
}
.date-form button:hover, .secondary-btn:hover { background: var(--asu-maroon); color: #fff; }

.sidebar .date-form { flex-direction: column; align-items: stretch; gap: 0.35rem; margin-top: 0; }
.sidebar .date-form label { font-size: 0.8rem; color: var(--text-muted); }
.sidebar .secondary-form { display: block; margin-top: 0.75rem; }
.sidebar .secondary-btn { width: 100%; padding: 0.5rem 0.7rem; }
.sidebar .delete-form { display: block; margin-top: 1rem; }
.sidebar .delete-btn { width: 100%; padding: 0.5rem 0.7rem; }

.admin-form { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0 1.5rem; align-items: center; }
.admin-form input:not([type="checkbox"]), .admin-form select {
  padding: 0.6rem 0.8rem; border: 1px solid var(--border); border-radius: 8px;
  background: rgba(255, 255, 255, 0.9); color: var(--text-primary); flex: 1; min-width: 200px; font-size: 1rem;
}
.admin-form input[type="number"] { flex: 0 0 auto; min-width: 0; width: 5rem; }
.admin-form select { flex: 0 0 auto; min-width: 0; width: auto; }
.admin-form .checkbox-label {
  flex: 0 0 auto; display: flex; align-items: center; gap: 0.4rem;
  font-size: 0.9rem; color: var(--text-muted); white-space: nowrap;
}
.admin-form .checkbox-label input[type="checkbox"] { width: auto; }
.admin-form input:focus, .admin-form select:focus { outline: 2px solid var(--asu-gold); border-color: var(--asu-maroon); }
.admin-form button {
  padding: 0.6rem 1rem; border: none; border-radius: 8px;
  background: var(--asu-maroon); color: #fff; font-weight: 600; cursor: pointer;
  box-shadow: 0 10px 25px -15px rgba(140, 29, 64, 0.8);
  transition: transform 0.2s ease, background 0.2s ease, color 0.2s ease;
}
.admin-form button:hover { background: var(--asu-gold); color: var(--asu-maroon); transform: translateY(-1px); }
.admin-table { width: 100%; border-collapse: collapse; }
.admin-table th, .admin-table td {
  text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); font-size: 0.9rem;
}
.admin-table th { color: var(--text-muted); font-weight: 600; }
.embed-code-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.copy-btn { font-size: 0.78rem; padding: 0.3rem 0.6rem; }
.cancel-link { align-self: center; color: var(--text-muted); font-size: 0.85rem; text-decoration: underline; }
.edit-link {
  color: var(--asu-maroon); font-size: 0.8rem; font-weight: 600; text-decoration: none;
  margin-right: 0.75rem;
}
.edit-link:hover { text-decoration: underline; }

.permalink-meta { margin-bottom: 1rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
.permalink-title-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.permalink-meta h1 {
  margin: 0 0 0.5rem; font-size: 1.4rem;
  color: var(--asu-black); background: var(--asu-gold); padding: 0.2rem 0.5rem; display: inline-block;
}
.permalink-meta .meta { font-size: 0.9rem; }
.print-btn { display: inline-flex; align-items: center; gap: 0.4rem; flex: 0 0 auto; white-space: nowrap; }
.print-btn svg { display: block; }

#body-frame { width: 100%; border: 0; display: block; min-height: 1400px; }
.empty { color: var(--text-muted); padding: 2rem 0; }

/* Print / "Save as PDF": clicking .print-btn calls window.print(), and this is what
   makes that show only the newsletter body itself -- everything else on the page
   (site chrome, admin sidebar, the meta line and button, embed footer link) is hidden
   for the print media type specifically, leaving only the actual archived content.
   Applies identically on the authenticated permalink page and the public embed
   permalink page, since both render this same template. */
@media print {
  .site-header, .app-footer, .permalink-meta, .admin-sidebar { display: none !important; }
  body { background: #fff; }
  main { box-shadow: none; border: none; border-radius: 0; padding: 0; margin: 0; max-width: 100%; }
  .layout { display: block; }
  .content { width: 100%; }
  #body-frame { min-height: 0; }
}

.app-footer {
  display: flex; flex-direction: column; align-items: center;
  gap: 0.5rem; padding: 0.75rem 1rem;
}
.app-footer__buttons { display: flex; align-items: center; gap: 0.75rem; }
.app-footer__credit {
  margin: 0; font-size: 0.7rem; color: var(--text-muted); text-align: center;
}
.app-footer__btn {
  flex: 0 0 auto; color: #fff; background: var(--asu-maroon);
  font-weight: 700; font-size: 0.75rem; padding: 0.4rem 1rem;
  border-radius: 999px; text-decoration: none; white-space: nowrap;
}
.app-footer__btn:hover { background: var(--asu-black); color: #fff; text-decoration: none; }
.app-footer__social {
  display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto;
  width: 1.75rem; height: 1.75rem; border-radius: 999px;
  background: var(--asu-maroon); color: #fff; text-decoration: none;
}
.app-footer__social:hover { background: var(--asu-black); color: #fff; text-decoration: none; }
.app-footer__social svg { display: block; }

.about-logo { display: block; height: 80px; width: auto; border-radius: 4px; }
.source-repo { font-weight: 600; word-break: break-all; }

.visibility-badge {
  display: inline-block; font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.04em; padding: 0.1rem 0.45rem; border-radius: 999px;
  background: var(--asu-sand); color: var(--asu-maroon); vertical-align: middle;
}
.visibility-badge--private { background: var(--asu-maroon); color: #fff; }
.share-link {
  display: block; font-size: 0.72rem; background: var(--asu-sand); color: var(--text-primary);
  padding: 0.3rem 0.5rem; border-radius: 6px; margin-bottom: 0.4rem; word-break: break-all;
}

.admin-footer {
  display: flex; align-items: center; justify-content: center; gap: 1rem;
  padding: 0.6rem 1rem; background: var(--asu-sand); font-size: 0.8rem;
}
.admin-footer-label {
  background: var(--asu-maroon); color: #fff; font-weight: 700;
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em;
  padding: 0.25rem 0.6rem; border-radius: 999px;
}
.admin-footer a { font-weight: 600; }
"""

_TEMPLATES = {
    "base.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}The 1885 Post{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="icon" href="/favicon.ico">
  {% block head %}{% endblock %}
</head>
<body>
  <header class="site-header">
    <div class="site-header-inner {{ self.main_class() }}">
      {% if embed_back_url %}<div class="site-brand">{% else %}<a class="site-brand" href="/">{% endif %}
        <img src="/static/app-mark.png" alt="" aria-hidden="true" class="site-mark" width="82" height="50">
        <span class="site-titles">
          <span class="site-title">The 1885 Post</span>
          <span class="site-subtitle">An Arizona State University newsletter archive</span>
        </span>
      {% if embed_back_url %}</div>{% else %}</a>{% endif %}
      <div class="header-right">
        {% if identity_display %}
          <nav class="site-nav">
            <a class="nav-link" href="/archive">Archive</a>
            <a class="nav-link" href="/embeds">Embeds</a>
            <a class="nav-link" href="/permissions">Permissions</a>
          </nav>
        {% elif embed_back_url %}
          <a class="nav-link" href="{{ embed_back_url }}">{{ embed_back_label }}</a>
        {% endif %}
        {% if identity_display %}
          <div class="identity">
            <div class="identity-name">{{ identity_display }}</div>
            {% if identity_email and identity_email != identity_display %}
              <div class="identity-email">{{ identity_email }}</div>
            {% endif %}
          </div>
        {% endif %}
      </div>
    </div>
  </header>
  <main class="{% block main_class %}{% endblock %}">
    {% block content %}{% endblock %}
  </main>
  <footer class="app-footer">
    <div class="app-footer__buttons">
      <a href="/about" class="app-footer__btn">About</a>
      <a href="/help" class="app-footer__btn">Help</a>
      <a href="https://github.com/ASU-Center-for-Evolution-and-Medicine/1885-post" class="app-footer__btn" target="_blank" rel="noopener noreferrer" title="Source code for this site on GitHub">Source code</a>
      <a class="app-footer__social" href="https://www.linkedin.com/company/asu-center-for-evolution-medicine/" target="_blank" rel="noopener noreferrer" title="LinkedIn" aria-label="Center for Evolution and Medicine on LinkedIn">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <rect x="2" y="2" width="20" height="20" rx="4" stroke="currentColor" stroke-width="2"></rect>
          <circle cx="7.3" cy="7.8" r="1.4" fill="currentColor"></circle>
          <rect x="6.2" y="10.6" width="2.3" height="7.2" fill="currentColor"></rect>
          <rect x="10.6" y="10.6" width="2.3" height="7.2" fill="currentColor"></rect>
          <path d="M12.9 13.6c1.4-2 5-1.5 5 1.6v2.6" stroke="currentColor" stroke-width="2.3" fill="none"></path>
        </svg>
      </a>
      <a class="app-footer__social" href="https://www.youtube.com/evolutionarymedicine" target="_blank" rel="noopener noreferrer" title="YouTube" aria-label="Center for Evolution and Medicine on YouTube">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <rect x="1.8" y="5" width="20.4" height="14" rx="4.5" stroke="currentColor" stroke-width="2"></rect>
          <path d="M10.2 9.3 15.6 12 10.2 14.7Z" fill="currentColor"></path>
        </svg>
      </a>
    </div>
    <p class="app-footer__credit">Built and maintained by the
    <a href="https://evmed.asu.edu/">Center for Evolution and Medicine</a> at ASU</p>
  </footer>
  {% if is_super_admin %}
    <footer class="admin-footer">
      <span class="admin-footer-label">Superadmin</span>
      <a href="/admin/newsletters">All newsletters</a>
      <a href="/admin/actions">Action log</a>
      <a href="/quarantine">Quarantine</a>
      <a href="/deleted">Deleted</a>
    </footer>
  {% endif %}
</body>
</html>
""",
    "home.html": """{% extends "base.html" %}
{% block title %}The 1885 Post{% endblock %}
{% block main_class %}wide{% endblock %}
{% block content %}
  {% if senders %}
    <div class="sender-grid">
      {% for s in senders %}
        <a class="sender-card" href="/archive?sender={{ s.from_email }}">
          <div class="sender-card-thumb-wrap">
            {% if s.latest_thumbnail_key %}
              <img class="sender-card-thumb" src="/static/newsletters/{{ s.latest_slug }}/{{ s.latest_thumbnail_key }}" alt="">
            {% else %}
              <div class="sender-card-thumb sender-card-thumb--placeholder">{{ s.name[0]|upper }}</div>
            {% endif %}
            <span class="sender-card-badge">{{ s.count }}</span>
          </div>
          <div class="sender-card-body">
            <div class="sender-card-name">{{ s.name }}<span class="sender-card-arrow">&rarr;</span></div>
            <div class="sender-card-meta">latest {{ s.latest_received_at|humandate }}</div>
          </div>
        </a>
      {% endfor %}
      <a class="sender-card view-all-card" href="/archive">
        <div class="view-all-main">
          <span class="view-all-text">View all</span>
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <line x1="3" y1="10" x2="15" y2="10" stroke="currentColor" stroke-width="2" stroke-linecap="round"></line>
            <polyline points="9 4 15 10 9 16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"></polyline>
          </svg>
        </div>
        <span class="view-all-stats">{{ total_newsletters }} newsletter{{ "s" if total_newsletters != 1 }} &middot; {{ total_senders }} sender{{ "s" if total_senders != 1 }}</span>
      </a>
    </div>
  {% else %}
    <p class="empty">No newsletters archived yet.</p>
  {% endif %}
{% endblock %}
""",
    "list.html": """{% extends "base.html" %}
{% block title %}Archive · The 1885 Post{% endblock %}
{% block main_class %}wide{% endblock %}
{% block content %}
  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar-section">
        <h2>Sort</h2>
        <a class="sidebar-link {{ 'active' if filters.sort != 'oldest' }}" href="?sender={{ filters.sender }}&sort=newest">Newest first</a>
        <a class="sidebar-link {{ 'active' if filters.sort == 'oldest' }}" href="?sender={{ filters.sender }}&sort=oldest">Oldest first</a>
      </div>
      <div class="sidebar-section">
        <h2>Senders</h2>
        <a class="sidebar-link {{ 'active' if not filters.sender }}" href="?sort={{ filters.sort }}">All senders</a>
        {% for s in senders %}
          <a class="sidebar-link {{ 'active' if filters.sender == s.from_email }}" href="?sender={{ s.from_email }}&sort={{ filters.sort }}">
            <span>{{ s.name }} <span class="count">({{ s.count }})</span></span>
            <span class="sender-email">{{ s.from_email }}</span>
          </a>
        {% endfor %}
      </div>
    </aside>

    <div class="content">
      {% if newsletters %}
        <ul class="newsletter-list">
          {% for n in newsletters %}
            <li>
              <a class="thumb-link" href="/n/{{ n.slug }}">
                {% if n.thumbnail_key %}
                  <img class="thumb" src="/static/newsletters/{{ n.slug }}/{{ n.thumbnail_key }}" alt="">
                {% else %}
                  <div class="thumb-placeholder">{{ (n.from_address or "?")[0]|upper }}</div>
                {% endif %}
              </a>
              <div class="li-text">
                <a class="subject" href="/n/{{ n.slug }}">{{ n.subject }}</a>{% if n.visibility != "public" %} <span class="visibility-badge visibility-badge--private">Private</span>{% endif %}
                <div class="meta">
                  {{ n.from_address }} &middot; {{ (n.received_at or n.created_at)|humandate }}
                  {% if is_super_admin or n.from_email in admin_senders %}
                    &middot;
                    <form class="delete-form" method="post" action="/n/{{ n.slug }}/delete" onsubmit="return confirm('Delete this newsletter? (can be restored from the Deleted page)');">
                      <button type="submit" class="delete-btn">Delete</button>
                    </form>
                  {% endif %}
                </div>
              </div>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="empty">No newsletters archived yet.</p>
      {% endif %}

      <div class="pagination">
        {% if page > 1 %}
          <a class="page-btn" href="?sender={{ filters.sender }}&sort={{ filters.sort }}&page={{ page - 1 }}">&larr; Newer</a>
        {% endif %}
        {% for item in pagination_items %}
          {% if item is none %}
            <span class="page-ellipsis">&hellip;</span>
          {% elif item == page %}
            <span class="page-btn active">{{ item }}</span>
          {% else %}
            <a class="page-btn" href="?sender={{ filters.sender }}&sort={{ filters.sort }}&page={{ item }}">{{ item }}</a>
          {% endif %}
        {% endfor %}
        {% if has_next %}
          <a class="page-btn" href="?sender={{ filters.sender }}&sort={{ filters.sort }}&page={{ page + 1 }}">Older &rarr;</a>
        {% endif %}
      </div>
    </div>
  </div>
{% endblock %}
""",
    "permalink.html": """{% extends "base.html" %}
{% block title %}{{ newsletter.subject }} · The 1885 Post{% endblock %}
{% block head %}
  <meta property="og:type" content="article">
  <meta property="og:site_name" content="The 1885 Post">
  <meta property="og:title" content="{{ newsletter.subject }}">
  <meta property="og:description" content="Newsletter from {{ newsletter.from_address }} — archived by The 1885 Post.">
  <meta property="og:url" content="{{ canonical_url }}">
  {% if newsletter.thumbnail_key %}
    <meta property="og:image" content="{{ base_url }}/static/newsletters/{{ newsletter.slug }}/{{ newsletter.thumbnail_key }}">
    <meta name="twitter:card" content="summary_large_image">
  {% else %}
    <meta name="twitter:card" content="summary">
  {% endif %}
{% endblock %}
{% block main_class %}wide{% endblock %}
{% block content %}
  <div class="layout">
    <div class="content">
      <div class="permalink-meta">
        <div class="permalink-title-row">
          <h1>{{ newsletter.subject }}</h1>
          <button type="button" class="secondary-btn print-btn" onclick="window.print()">
            <svg width="15" height="15" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect x="5" y="1.5" width="10" height="5" stroke="currentColor" stroke-width="1.5"></rect>
              <rect x="2" y="6.5" width="16" height="8" rx="1.5" stroke="currentColor" stroke-width="1.5"></rect>
              <rect x="5" y="12" width="10" height="6.5" stroke="currentColor" stroke-width="1.5" fill="currentColor" fill-opacity="0.08"></rect>
              <circle cx="15" cy="9" r="0.9" fill="currentColor"></circle>
            </svg>
            Print
          </button>
        </div>
        <div class="meta">From {{ newsletter.from_address }} &middot; {{ (newsletter.received_at or newsletter.created_at)|humandate }}
          {% if identity_display %}
            &middot;
            {% if newsletter.visibility == "public" %}
              <span class="visibility-badge">Public</span>
            {% else %}
              <span class="visibility-badge visibility-badge--private">Private</span>
            {% endif %}
          {% endif %}
        </div>
      </div>

      {% if newsletter.sanitized_html %}
        <iframe id="body-frame" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" srcdoc="{{ newsletter.sanitized_html }}"></iframe>
        <script>
      const frame = document.getElementById("body-frame");

      function measure(doc) {
        const el = doc.documentElement;
        const body = doc.body;
        let max = Math.max(
          el ? el.scrollHeight : 0,
          el ? el.offsetHeight : 0,
          body ? body.scrollHeight : 0,
          body ? body.offsetHeight : 0
        );
        // scrollHeight/offsetHeight can be lied to by a wrapper div the newsletter set
        // its own height+overflow on (common for their own preview-pane use case) --
        // our override below only reaches html/body, not arbitrary wrapper elements,
        // and a vh/%-based wrapper height can also self-stabilize too small (it
        // resolves against the iframe's *current*, still-small, viewport). Fall back to
        // the true bottom edge of the deepest-laid-out element: getBoundingClientRect
        // reflects real layout position regardless of any ancestor's overflow clipping.
        if (body) {
          for (const child of body.querySelectorAll("*")) {
            const bottom = child.getBoundingClientRect().bottom;
            if (bottom > max) max = bottom;
          }
        }
        return Math.ceil(max);
      }

      function resize() {
        const doc = frame.contentDocument;
        if (!doc) return;
        frame.style.height = measure(doc) + "px";
      }

      frame.addEventListener("load", () => {
        try {
          const doc = frame.contentDocument;
          if (!doc) return;

          const style = doc.createElement("style");
          style.textContent =
            "html, body { height: auto !important; min-height: 0 !important; overflow: visible !important; }";
          (doc.head || doc.documentElement).appendChild(style);

          resize();

          // Images/web fonts can still reflow content after `load` fires, and a
          // vh/%-sized wrapper may need a couple of iterations to converge once the
          // frame itself grows -- keep re-measuring as that settles rather than
          // trusting a single one-shot read.
          if (doc.body && window.ResizeObserver) {
            new ResizeObserver(resize).observe(doc.body);
          }
          // Tie re-measurement directly to each image's own load/error event, rather
          // than only guessing with fixed delays -- covers slow/external image hosts
          // that could still be settling after `load` fires in some browsers.
          Array.from(doc.images || []).forEach((img) => {
            if (!img.complete) {
              img.addEventListener("load", resize);
              img.addEventListener("error", resize);
            }
          });
          setTimeout(resize, 300);
          setTimeout(resize, 1000);
          setTimeout(resize, 2500);
        } catch (err) {
          // Never leave the frame stuck at the browser's tiny default height because
          // of an unexpected error in here -- the CSS min-height fallback covers it.
          console.error("newsletter iframe resize failed", err);
        }
      });
        </script>
      {% elif newsletter.plain_text_fallback %}
        <pre>{{ newsletter.plain_text_fallback }}</pre>
      {% else %}
        <p class="empty">This newsletter had no readable body.</p>
      {% endif %}
    </div>

    {% if is_super_admin or is_admin %}
      <aside class="sidebar admin-sidebar">
        <div class="sidebar-section">
          <h2>Sharing</h2>
          {% if newsletter.visibility == "public" %}
            <p class="meta">Public -- appears in embeds and can be shared publicly.</p>
            {% if sender_share_key %}
              <code class="share-link">{{ base_url }}/embed/s/{{ sender_share_key }}/n/{{ newsletter.slug }}</code>
              <button type="button" class="secondary-btn copy-btn" data-url="{{ base_url }}/embed/s/{{ sender_share_key }}/n/{{ newsletter.slug }}">Copy public link</button>
            {% else %}
              <p class="meta">This sender has no public sharing key yet.</p>
              <form class="secondary-form" method="post" action="/n/{{ newsletter.slug }}/share-key">
                <button type="submit" class="secondary-btn">Create public link</button>
              </form>
            {% endif %}
            <form class="secondary-form" method="post" action="/n/{{ newsletter.slug }}/visibility">
              <input type="hidden" name="visibility" value="private">
              <button type="submit" class="secondary-btn">Make private</button>
            </form>
          {% else %}
            <p class="meta">Private -- hidden from every embed and has no public link.
            Still visible to signed-in users here.</p>
            <form class="secondary-form" method="post" action="/n/{{ newsletter.slug }}/visibility">
              <input type="hidden" name="visibility" value="public">
              <button type="submit" class="secondary-btn">Make public</button>
            </form>
          {% endif %}
        </div>
        <div class="sidebar-section">
          <h2>Admin</h2>
          <form class="date-form" method="post" action="/n/{{ newsletter.slug }}/date">
            <label for="received_at">Send date</label>
            <input type="date" id="received_at" name="received_at" value="{{ (newsletter.received_at or newsletter.created_at)[:10] }}">
            <button type="submit">Update date</button>
          </form>
          <form class="secondary-form" method="post" action="/n/{{ newsletter.slug }}/reprocess" title="Re-run link resolution and unsubscribe-link cleanup against the original email">
            <button type="submit" class="secondary-btn">Reprocess links</button>
          </form>
          <form class="delete-form" method="post" action="/n/{{ newsletter.slug }}/delete" onsubmit="return confirm('Delete this newsletter? (can be restored from the Deleted page)');">
            <button type="submit" class="delete-btn">Delete this newsletter</button>
          </form>
        </div>
      </aside>
      <script>
        document.querySelectorAll(".copy-btn").forEach((btn) => {
          btn.addEventListener("click", () => {
            navigator.clipboard.writeText(btn.dataset.url).then(() => {
              const original = btn.textContent;
              btn.textContent = "Copied!";
              setTimeout(() => { btn.textContent = original; }, 1500);
            });
          });
        });
      </script>
    {% endif %}
  </div>
{% endblock %}
""",
    "help.html": """{% extends "base.html" %}
{% block title %}Help · The 1885 Post{% endblock %}
{% block content %}
  <h1>Help</h1>

  <h2>What this is</h2>
  <p class="meta">The 1885 Post is a university-wide archive for ASU newsletters -- one
  permanent, linkable home for every department's newsletter, instead of each issue
  living only in inboxes and disappearing after it's sent.</p>

  <h2>How to use it</h2>
  <p class="meta">Send (or CC/BCC) your newsletter to <strong>newsletters@evmed.app</strong>
  when you send it out -- it appears on the <a href="/">homepage</a> automatically, no
  extra steps.</p>
  <p class="meta">To show a "recent newsletters" widget on your department's website, go
  to <a href="/embeds">Embeds</a>, fill in a name and the sender to show, then click
  <strong>Create embed</strong> and paste the copied code into your site. Any logged-in
  user can create one, no admin access needed.</p>
  <p class="meta"><strong>Public or private:</strong> every newsletter is one or the
  other, shown next to its date. Public means it appears in embeds and can be shared
  outside ASU; private means it's visible to signed-in people here but nowhere else --
  not in any embed, and with no public link. If you administer a sender, each newsletter
  has a <strong>Make private</strong> / <strong>Make public</strong> button.</p>
  <p class="meta"><strong>What new mail defaults to.</strong> If somebody already
  administers your sending address, new newsletters from it arrive public, exactly as
  before. If nobody does yet, they arrive <em>private</em> -- archived and visible here,
  but not published -- so an unfamiliar address can't put content in front of the
  outside world just by emailing in. Under Sender settings on
  <a href="/permissions">Permissions</a> you can set your own address's default either
  way, which overrides that.</p>
  <p class="meta"><strong>Sharing one newsletter:</strong> open it and use
  <strong>Copy public link</strong> -- no embed needed. The first time, click
  <strong>Create public link</strong> to mint your sender's sharing key; every public
  newsletter from that address is shareable from then on, and revoking the key on
  <a href="/permissions">Permissions</a> switches all those links off at once.</p>
  <p class="meta"><strong>Permissions:</strong> admin access is per sending address -- it
  lets you delete, backdate, publish, and manage embeds for newsletters from that
  sender, even ones you didn't create yourself. It's not needed just to create your own
  embeds. See <a href="/permissions">Permissions</a> for who currently administers which
  sender.</p>

  <h2>What happens behind the scenes</h2>
  <p class="meta">A few things happen automatically when a newsletter is archived, so
  the copy you see months or years later still works the way it did on day one:</p>
  <p class="meta"><strong>Links get updated.</strong> Newsletter platforms (Mailchimp,
  Constant Contact, etc.) often wrap every link in a click-tracking redirect -- the
  archive follows those once and stores the real destination instead, so archived links
  keep working even after the original campaign's tracking expires. Unsubscribe /
  manage-preferences links are neutralized for the same reason in reverse: so the public
  archive can never be used to unsubscribe someone.</p>
  <p class="meta"><strong>Images are copied, not just linked.</strong> Externally-hosted
  images are downloaded once and stored permanently as part of the archive, rather than
  left pointing at wherever they originally lived -- so the newsletter still renders
  correctly even if the sending platform later deletes or expires them.</p>
  <p class="meta"><strong>Two gates stand between arriving and being published.</strong>
  First, anything from outside <code>*.asu.edu</code> is held in a quarantine --
  invisible on the homepage, the archive, and every embed -- until a super admin
  whitelists that sending address. Second, an <code>*.asu.edu</code> newsletter is
  published automatically only if somebody already administers its sending address;
  otherwise it lands private, and an admin publishes it when they're ready. Nothing is
  ever silently rejected or dropped either way, so a newsletter that hasn't shown up
  where you expected is always findable. If your department sends through a third-party
  platform under a different domain, or your sending address is new here, contact
  Suhail.</p>

  <h2>Contact</h2>
  <p class="meta">Questions, need admin access, or have a feature request? Contact Suhail
  (<a href="mailto:suhail.ghafoor@asu.edu">suhail.ghafoor@asu.edu</a>).</p>
{% endblock %}
""",
    "about.html": """{% extends "base.html" %}
{% block title %}About · The 1885 Post{% endblock %}
{% block content %}
  <h1>About</h1>

  <p class="meta">The 1885 Post is a university-wide archive for ASU newsletters -- one
  permanent, linkable home for every department's newsletter, instead of each issue
  living only in inboxes and disappearing after it's sent.</p>

  <p><a href="https://evmed.asu.edu"><img src="/static/logo.png" alt="Center for Evolution and Medicine logo" class="about-logo" width="220" height="55"></a></p>

  <p class="meta">Made by the <a href="https://evmed.asu.edu/">Center for Evolution and
  Medicine</a> at <a href="https://asu.edu">Arizona State University</a>.</p>

  <h2>Source code</h2>
  <p class="meta">This site is open source. The code that runs The 1885 Post itself --
  how newsletters are received, sanitized, stored, and shared -- is published for
  transparency in its own repository:</p>
  <p class="meta"><a class="source-repo" href="https://github.com/ASU-Center-for-Evolution-and-Medicine/1885-post" target="_blank" rel="noopener noreferrer">github.com/ASU-Center-for-Evolution-and-Medicine/1885-post</a></p>
{% endblock %}
""",
    "permissions.html": """{% extends "base.html" %}
{% block title %}Permissions · The 1885 Post{% endblock %}
{% block content %}
  <h1>Permissions</h1>

  <p class="meta">Admin rights are granted per sender (from) address: an admin can
  delete newsletters *from* that sender, backdate them, and edit/revoke embeds scoped to
  it even if someone else created them. Every authenticated user can already view every
  newsletter and create their own embeds for any sender regardless of grants -- this page
  just lists, for full transparency, who additionally administers which sender.</p>

  {% if is_super_admin %}
    <form class="admin-form" method="post" action="/permissions/grants">
      <input type="email" name="user_email" placeholder="user@example.com" required>
      <input type="text" name="from_email" placeholder="news@sender.com" required>
      <button type="submit">Grant</button>
    </form>
  {% endif %}

  {% if grants %}
    <table class="admin-table">
      <thead><tr><th>User</th><th>Sender (from)</th>{% if is_super_admin %}<th></th>{% endif %}</tr></thead>
      <tbody>
        {% for g in grants %}
          <tr>
            <td>{{ g.user_email }}</td>
            <td>{{ g.from_email }}</td>
            {% if is_super_admin %}
              <td>
                <form class="delete-form" method="post" action="/permissions/grants/{{ g.id }}/delete" onsubmit="return confirm('Revoke this grant?');">
                  <button type="submit" class="delete-btn">Revoke</button>
                </form>
              </td>
            {% endif %}
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No admin grants yet.</p>
  {% endif %}

  {% if sender_settings %}
    <h2>Sender settings</h2>
    <p class="meta">For senders you administer. <strong>Default</strong> decides whether
    a newly-arrived newsletter from that address is public (in embeds, publicly
    shareable) or private (signed-in users only) -- you can still flip any individual
    newsletter afterwards. <strong>Public sharing</strong> mints one unguessable link
    per sender, used for every "Copy public link" from that sender; revoking it
    immediately breaks every link previously shared under it.</p>
    <table class="admin-table">
      <thead><tr><th>Sender</th><th>Default for new newsletters</th><th>Public sharing</th></tr></thead>
      <tbody>
        {% for s in sender_settings %}
          <tr>
            <td>{{ s.name }}<div class="sender-email">{{ s.from_email }}</div></td>
            <td>
              <form class="date-form" method="post" action="/permissions/senders/default">
                <input type="hidden" name="from_email" value="{{ s.from_email }}">
                <select name="visibility">
                  <option value="public" {{ "selected" if s.default_visibility == "public" }}>Public</option>
                  <option value="private" {{ "selected" if s.default_visibility == "private" }}>Private</option>
                </select>
                <button type="submit">Save</button>
              </form>
              {% if not s.is_explicit %}
                <div class="meta">inherited &mdash; not set explicitly</div>
              {% endif %}
            </td>
            <td>
              {% if s.share_key %}
                <code class="share-link">{{ base_url }}/embed/s/{{ s.share_key }}</code>
                <form class="delete-form" method="post" action="/permissions/senders/share-key" onsubmit="return confirm('Revoke this sharing key? Every public link already shared for this sender stops working.');">
                  <input type="hidden" name="from_email" value="{{ s.from_email }}">
                  <input type="hidden" name="action" value="revoke">
                  <button type="submit" class="delete-btn">Revoke key</button>
                </form>
              {% else %}
                <form class="secondary-form" method="post" action="/permissions/senders/share-key">
                  <input type="hidden" name="from_email" value="{{ s.from_email }}">
                  <input type="hidden" name="action" value="mint">
                  <button type="submit" class="secondary-btn">Create sharing key</button>
                </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}
{% endblock %}
""",
    "quarantine.html": """{% extends "base.html" %}
{% block title %}Quarantine · The 1885 Post{% endblock %}
{% block content %}
  <h1>Quarantine</h1>

  <p class="meta">Newsletters from senders outside *.asu.edu land here instead of the
  homepage, archive, or any embed (including "all senders" embeds) -- nothing is
  rejected or silently dropped, so anything that comes in stays visible either here or
  in the normal archive.</p>

  {% if quarantined %}
    <table class="admin-table">
      <thead><tr><th>Subject</th><th>From</th><th>Received</th><th></th></tr></thead>
      <tbody>
        {% for n in quarantined %}
          <tr>
            <td><a href="/n/{{ n.slug }}">{{ n.subject }}</a></td>
            <td>{{ n.from_address }}</td>
            <td>{{ (n.received_at or n.created_at)|humandate }}</td>
            <td>
              <form class="delete-form" method="post" action="/quarantine/{{ n.slug }}/release">
                <button type="submit" class="secondary-btn">Release</button>
              </form>
              <form class="delete-form" method="post" action="/quarantine/{{ n.slug }}/whitelist" onsubmit="return confirm('Whitelist this sender and release all their quarantined newsletters?');">
                <button type="submit" class="secondary-btn">Whitelist sender</button>
              </form>
              <form class="delete-form" method="post" action="/n/{{ n.slug }}/delete" onsubmit="return confirm('Delete this newsletter? (can be restored from the Deleted page)');">
                <button type="submit" class="delete-btn">Delete</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">Nothing in quarantine.</p>
  {% endif %}

  <h2>Sender allowlist</h2>
  <p class="meta">Non-ASU addresses here skip quarantine entirely, for both past and
  future newsletters.</p>
  <form class="admin-form" method="post" action="/quarantine/allowlist">
    <input type="email" name="email" placeholder="vendor@example.com" required>
    <button type="submit">Allow</button>
  </form>

  {% if allowlist %}
    <table class="admin-table">
      <thead><tr><th>Email</th><th>Added</th><th></th></tr></thead>
      <tbody>
        {% for a in allowlist %}
          <tr>
            <td>{{ a.email }}</td>
            <td>{{ a.created_at|humandate }}</td>
            <td>
              <form class="delete-form" method="post" action="/quarantine/allowlist/{{ a.id }}/delete" onsubmit="return confirm('Remove from allowlist?');">
                <button type="submit" class="delete-btn">Remove</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No allowlisted senders yet.</p>
  {% endif %}
{% endblock %}
""",
    "deleted.html": """{% extends "base.html" %}
{% block title %}Deleted · The 1885 Post{% endblock %}
{% block content %}
  <h1>Deleted</h1>

  <p class="meta">Deleting a newsletter never actually erases it -- it's just hidden
  from the homepage, archive, and every embed, with a record of who deleted it and
  when. Restore anything below to bring it back exactly as it was.</p>

  {% if deleted %}
    <table class="admin-table">
      <thead><tr><th>Subject</th><th>From</th><th>Deleted by</th><th>Deleted</th><th></th></tr></thead>
      <tbody>
        {% for n in deleted %}
          <tr>
            <td><a href="/n/{{ n.slug }}">{{ n.subject }}</a></td>
            <td>{{ n.from_address }}</td>
            <td>{{ n.deleted_by }}</td>
            <td>{{ n.deleted_at|humandate }}</td>
            <td>
              <form class="delete-form" method="post" action="/deleted/{{ n.slug }}/restore">
                <button type="submit" class="secondary-btn">Restore</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">Nothing deleted.</p>
  {% endif %}
{% endblock %}
""",
    "admin_newsletters.html": """{% extends "base.html" %}
{% block title %}All Newsletters · The 1885 Post{% endblock %}
{% block content %}
  <h1>All Newsletters</h1>

  <p class="meta">Every newsletter ever ingested, ordered by when it was actually added
  to the archive (not the date on the email itself) -- including quarantined and
  deleted ones, shown below with their status.</p>

  {% if newsletters %}
    <table class="admin-table">
      <thead><tr><th>Subject</th><th>From</th><th>Created</th><th>Received</th><th>Visibility</th><th>Status</th></tr></thead>
      <tbody>
        {% for n in newsletters %}
          <tr>
            <td><a href="/n/{{ n.slug }}">{{ n.subject }}</a></td>
            <td>{{ n.from_address }}</td>
            <td>{{ n.created_at|humandate }}</td>
            <td>{{ (n.received_at or n.created_at)|humandate }}</td>
            <td>{{ "Public" if n.visibility == "public" else "Private" }}</td>
            <td>
              {% if n.deleted_at %}Deleted{% elif n.quarantined_at %}Quarantined{% endif %}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No newsletters yet.</p>
  {% endif %}

  <div class="pagination">
    {% if page > 1 %}
      <a class="page-btn" href="?page={{ page - 1 }}">&larr; Newer</a>
    {% endif %}
    {% for item in pagination_items %}
      {% if item is none %}
        <span class="page-ellipsis">&hellip;</span>
      {% elif item == page %}
        <span class="page-btn active">{{ item }}</span>
      {% else %}
        <a class="page-btn" href="?page={{ item }}">{{ item }}</a>
      {% endif %}
    {% endfor %}
    {% if has_next %}
      <a class="page-btn" href="?page={{ page + 1 }}">Older &rarr;</a>
    {% endif %}
  </div>
{% endblock %}
""",
    "admin_actions.html": """{% extends "base.html" %}
{% block title %}Action Log · The 1885 Post{% endblock %}
{% block content %}
  <h1>Action Log</h1>

  <p class="meta">A login row appears at most once per person per 4-hour session;
  every modification -- deleting/restoring/backdating/reprocessing a newsletter,
  creating/editing/revoking an embed, granting/revoking permissions, any quarantine
  action -- gets its own row showing who did it.</p>

  {% if entries %}
    <table class="admin-table">
      <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
      <tbody>
        {% for e in entries %}
          <tr>
            <td>{{ e.created_at|humandatetime }}</td>
            <td>{{ e.actor_email }}</td>
            <td>{{ e.action }}</td>
            <td>{{ e.target or "" }}</td>
            <td>{{ e.detail or "" }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No actions logged yet.</p>
  {% endif %}

  <div class="pagination">
    {% if page > 1 %}
      <a class="page-btn" href="?page={{ page - 1 }}">&larr; Newer</a>
    {% endif %}
    {% for item in pagination_items %}
      {% if item is none %}
        <span class="page-ellipsis">&hellip;</span>
      {% elif item == page %}
        <span class="page-btn active">{{ item }}</span>
      {% else %}
        <a class="page-btn" href="?page={{ item }}">{{ item }}</a>
      {% endif %}
    {% endfor %}
    {% if has_next %}
      <a class="page-btn" href="?page={{ page + 1 }}">Older &rarr;</a>
    {% endif %}
  </div>
{% endblock %}
""",
    "embeds.html": """{% extends "base.html" %}
{% block title %}Embeds · The 1885 Post{% endblock %}
{% block content %}
  <h1>Embeds</h1>

  <p class="meta">Publish a public, unauthenticated list of recent newsletters (optionally
  filtered by sender) for embedding as an iframe on a department website -- no login
  required to view it or open a newsletter from it. Any logged-in user can create one.
  Revoke to break every iframe using it immediately; edit to change what an existing
  embed shows without breaking its URL.</p>

  <form class="admin-form" method="post" action="{{ ('/embeds/' ~ edit_embed.token ~ '/edit') if edit_embed else '/embeds' }}">
    <input type="text" name="name" placeholder="Name, e.g. CEM homepage widget" value="{{ edit_embed.name if edit_embed else '' }}" required>
    <input type="text" name="sender_email" placeholder="Sender email (blank = all senders)" value="{{ edit_embed.sender_email or '' if edit_embed else '' }}">
    <input type="number" name="result_limit" value="{{ edit_embed.result_limit if edit_embed else 5 }}" min="0" max="50" title="0 shows all newsletters">
    <select name="sort">
      <option value="newest" {{ "selected" if edit_embed and edit_embed.sort != "oldest" }}>Newest first</option>
      <option value="oldest" {{ "selected" if edit_embed and edit_embed.sort == "oldest" }}>Oldest first</option>
    </select>
    <label class="checkbox-label">
      <input type="checkbox" name="show_thumbnails" {{ "checked" if edit_embed and edit_embed.show_thumbnails }}>
      Show thumbnails
    </label>
    <button type="submit">{{ "Save changes" if edit_embed else "Create embed" }}</button>
    {% if edit_embed %}<a href="/embeds" class="cancel-link">Cancel</a>{% endif %}
  </form>
  <p class="meta" style="margin-top: -0.75rem;">Limit: 0 shows every matching newsletter.
  New embeds default to no thumbnails, so existing embeds elsewhere never change
  appearance on their own.</p>

  {% if embeds %}
    <table class="admin-table">
      <thead><tr><th>Name</th><th>Sender</th><th>Shows</th><th>Created by</th><th>Embed code</th><th></th></tr></thead>
      <tbody>
        {% for e in embeds %}
          <tr>
            <td>{{ e.name }}</td>
            <td>{{ e.sender_email or "All senders" }}</td>
            <td>{{ "All" if e.result_limit == 0 else "Last " ~ e.result_limit }}, {{ "oldest" if e.sort == "oldest" else "newest" }} first</td>
            <td>{{ e.created_by }}</td>
            <td>
              <div class="embed-code-actions">
                <button type="button" class="secondary-btn copy-btn copy-iframe-btn" data-url="{{ base_url }}/embed/{{ e.token }}?embedded=1">Copy iframe code</button>
                <button type="button" class="secondary-btn copy-btn copy-link-btn" data-url="{{ base_url }}/embed/{{ e.token }}">Copy link</button>
              </div>
            </td>
            <td>
              <a href="/embeds?edit={{ e.token }}" class="edit-link">Edit</a>
              <form class="delete-form" method="post" action="/embeds/{{ e.token }}/delete" onsubmit="return confirm('Revoke this embed? Any iframe using it will break immediately.');">
                <button type="submit" class="delete-btn">Revoke</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No embeds yet.</p>
  {% endif %}

  <script>
    document.querySelectorAll(".copy-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const text = btn.classList.contains("copy-iframe-btn")
          ? `<iframe src="${btn.dataset.url}" width="100%" height="400" style="border:0;"></iframe>`
          : btn.dataset.url;
        navigator.clipboard.writeText(text).then(() => {
          const original = btn.textContent;
          btn.textContent = "Copied!";
          setTimeout(() => { btn.textContent = original; }, 1500);
        });
      });
    });
  </script>
{% endblock %}
""",
    "embed_list.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <title>{{ feed_title }} · The 1885 Post</title>
  <link rel="stylesheet" href="/static/style.css">
  <style>
    body { margin: 0; padding: 0.75rem 1rem; }
    .embed-list { list-style: none; margin: 0; padding: 0; }
    .embed-list li { padding: 0.6rem 0; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.75rem; }
    .embed-list li:last-child { border-bottom: none; }
    .embed-thumb { width: 48px; height: 48px; border-radius: 8px; object-fit: cover; flex: 0 0 auto; }
    .embed-list-text { min-width: 0; }
    .embed-date { color: var(--asu-maroon); font-weight: 700; font-size: 0.9rem; margin-bottom: 0.15rem; }
    .embed-list a { color: var(--text-primary); font-weight: 600; text-decoration: none; font-size: 0.95rem; }
    .embed-list a:hover { color: var(--asu-maroon); }
    /* Only applied when this page is opened directly (not inside a real embed iframe) --
       see _looks_embedded() in app.py. The default rules above stay untouched so an
       actual department-site embed never changes. */
    body.standalone {
      max-width: 640px; margin: 2.5rem auto; padding: 1.5rem 1.75rem;
      background: var(--card-bg); border: 1px solid var(--border);
      border-radius: 16px; box-shadow: 0 25px 60px -30px rgba(140, 29, 64, 0.35);
    }
    .embed-standalone-header {
      display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem;
      padding-bottom: 0.75rem; border-bottom: 1px solid var(--border);
      color: var(--asu-maroon); font-weight: 700; font-size: 0.85rem;
    }
    @media (max-width: 700px) {
      body.standalone { margin: 0; border-radius: 0; }
    }
  </style>
</head>
<body class="{{ 'standalone' if standalone else '' }}">
  {% if standalone %}
    <div class="embed-standalone-header">
      <img src="/static/app-mark.png" alt="" aria-hidden="true" width="32" height="19">
      <span>{{ "Showing newsletters from " ~ sender_name if sender_name else "The 1885 Post · public newsletter feed" }}</span>
    </div>
  {% endif %}
  {% if newsletters %}
    <ul class="embed-list">
      {% for n in newsletters %}
        <li>
          {% if show_thumbnails and n.thumbnail_key %}
            <img class="embed-thumb" src="/static/newsletters/{{ n.slug }}/{{ n.thumbnail_key }}" alt="" aria-hidden="true">
          {% endif %}
          <div class="embed-list-text">
            <div class="embed-date">{{ (n.received_at or n.created_at)|humandate }}</div>
            <a href="{{ item_url_prefix }}/n/{{ n.slug }}" target="_blank" rel="noopener">{{ n.subject }}</a>
          </div>
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="empty">No newsletters yet.</p>
  {% endif %}
</body>
</html>
""",
}

def _humandate(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    # Strip the leading zero from day-of-month without relying on the non-portable
    # `%-d` strftime extension (unsupported on some libc/Emscripten targets).
    return dt.strftime("%b %d, %Y").replace(" 0", " ")


def _humandatetime(value: str | None) -> str:
    """Like _humandate but with a time -- for the action log, where same-day entries
    need to be distinguishable."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%b %d, %Y %H:%M UTC").replace(" 0", " ")


_jinja_env = jinja2.Environment(loader=jinja2.DictLoader(_TEMPLATES), autoescape=True)
_jinja_env.filters["humandate"] = _humandate
_jinja_env.filters["humandatetime"] = _humandatetime


def _render(template_name: str, **context) -> HTMLResponse:
    html = _jinja_env.get_template(template_name).render(**context)
    return HTMLResponse(html)


def _db(request: Request):
    return request.scope["env"].DB


def _bucket(request: Request):
    return request.scope["env"].ASSETS


async def _current_user(request: Request) -> tuple[str | None, str | None]:
    """(email, display_name) for the current request's Cloudflare Access identity.

    Identity comes from Cloudflare Access (see access.py) -- not from a client-supplied
    header. Every authenticated user can view every newsletter (Access already gates who
    reaches the site at all); a missing identity is the only thing that fails closed
    below, guarding the case where this Worker is reached some other way (e.g. its
    workers.dev URL) that Access doesn't protect.

    Also the one central hook for login logging (action_log): every authenticated route
    resolves identity through here, so this is where a "new session" gets recorded
    rather than duplicating that call at every route.
    """
    identity = await access.get_identity(request)
    email = access.identity_email(identity)
    display = access.display_name(identity)
    if email:
        await storage.log_login_if_new_session(_db(request), email)
    return email, display


def _is_super_admin(request: Request, email: str | None) -> bool:
    if not email:
        return False
    raw = getattr(request.scope["env"], "SUPER_ADMIN_EMAILS", "") or ""
    allowed = {addr.strip().lower() for addr in raw.split(",") if addr.strip()}
    return email.lower() in allowed


def _looks_embedded(request: Request) -> bool:
    """True if this request is genuinely loading /embed/{token} inside an iframe, as
    opposed to someone opening that same URL directly in a browser tab. Two independent
    signals, either sufficient: our own "Copy iframe code" snippet always appends
    ?embedded=1 (deterministic, works regardless of browser), and modern browsers send
    Sec-Fetch-Dest: iframe when fetching a document to place in an <iframe> (covers
    embeds pasted before this flag existed). Only affects cosmetic standalone styling in
    embed_list.html, never anything security-relevant."""
    if request.query_params.get("embedded") == "1":
        return True
    return request.headers.get("sec-fetch-dest") == "iframe"


async def _can_administer(request: Request, email: str, newsletter: storage.Newsletter) -> bool:
    """True if `email` may delete/edit this specific newsletter: super admin, or admin
    of its sender."""
    if _is_super_admin(request, email):
        return True
    admin_senders = await storage.list_admin_senders(_db(request), email)
    return newsletter.from_email in admin_senders


async def _parse_form(request: Request) -> dict[str, str]:
    """Minimal application/x-www-form-urlencoded parser -- avoids adding a dependency
    (python-multipart) just for two plain text fields on the admin grant form."""
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body)
    return {key: values[0] for key, values in parsed.items() if values}


@app.get("/static/style.css")
async def style_css():
    return Response(content=_CSS, media_type="text/css")


@app.get("/static/app-mark.png")
async def app_mark_png():
    return Response(content=base64.b64decode(APP_MARK_PNG_BASE64), media_type="image/png")


@app.get("/static/logo.png")
async def logo_png():
    return Response(content=base64.b64decode(LOGO_PNG_BASE64), media_type="image/png")


@app.get("/static/newsletters/{slug}/{key}")
async def view_mirrored_asset(request: Request, slug: str, key: str):
    """Serves an externally-hosted image mirrored into R2 at ingest/reprocess time (see
    mirror_external_image in worker_entry.py). Deliberately unauthenticated, like the
    other /static/* routes above -- this same sanitized_html is also rendered inside the
    public /embed/* permalink page, and an <img> reference in it is fetched by the
    visitor's browser directly against this path. Living under /static/* means it
    inherits whatever already makes /static/style.css and /static/app-mark.png reachable
    from a public embed (both referenced the same way from embed_list.html), rather than
    needing its own separate Access Bypass rule. `key` is a 20-hex-char hash of the
    original source URL (80 bits, not enumerable) and carries no more sensitivity than
    the newsletter body an embed already makes public."""
    obj = await _bucket(request).get(f"newsletters/{slug}/{key}")
    if obj is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Unlike js.Response.new(...).arrayBuffer() (a JsProxy needing .to_bytes(), used in
    # worker_entry.py's email() handler), R2's binding-based arrayBuffer() comes back
    # auto-converted to a plain Python memoryview here -- confirmed against the deployed
    # Worker, not documented anywhere obvious.
    data = (await obj.arrayBuffer()).tobytes()
    content_type = obj.httpMetadata.contentType or "application/octet-stream"
    return Response(content=data, media_type=content_type)


@app.get("/favicon.ico")
async def favicon_ico():
    return Response(content=base64.b64decode(FAVICON_ICO_BASE64), media_type="image/x-icon")


@app.get("/")
async def home_sender_cards(request: Request):
    """The homepage: one card per sender, showing their latest newsletter's thumbnail,
    how many newsletters they have archived, and the date of the latest one. The old
    full sortable/filterable/paginated list this replaced lives on at /archive
    (view_archive below)."""
    email, identity_display = await _current_user(request)
    if not email:
        return _render(
            "home.html",
            senders=[],
            identity_display=None,
            identity_email=None,
            is_super_admin=False,
        )

    senders = await storage.list_sender_cards(_db(request))
    return _render(
        "home.html",
        senders=senders,
        total_newsletters=sum(s.count for s in senders),
        total_senders=len(senders),
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=_is_super_admin(request, email),
    )


@app.get("/archive")
async def view_archive(
    request: Request,
    sender: str | None = None,
    sort: str = "newest",
    page: int = 1,
):
    """The full sortable/filterable/paginated list -- previously served at "/" before
    the homepage became the sender-card grid (home_sender_cards above)."""
    page = max(page, 1)
    sort = "oldest" if sort == "oldest" else "newest"
    email, identity_display = await _current_user(request)
    if not email:
        return _render(
            "list.html",
            newsletters=[],
            senders=[],
            filters={"sender": sender or "", "sort": sort},
            page=1,
            total_pages=1,
            pagination_items=[1],
            has_next=False,
            identity_display=None,
            identity_email=None,
            admin_senders=set(),
            is_super_admin=False,
        )

    total = await storage.count_newsletters(_db(request), sender=sender)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * _PAGE_SIZE

    rows = await storage.list_newsletters(
        _db(request),
        sender=sender,
        sort=sort,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    senders = await storage.list_senders(_db(request))
    admin_senders = set(await storage.list_admin_senders(_db(request), email))
    return _render(
        "list.html",
        newsletters=rows,
        senders=senders,
        filters={"sender": sender or "", "sort": sort},
        page=page,
        total_pages=total_pages,
        pagination_items=_pagination_items(page, total_pages),
        has_next=page < total_pages,
        identity_display=identity_display,
        identity_email=email,
        admin_senders=admin_senders,
        is_super_admin=_is_super_admin(request, email),
    )


@app.get("/n/{slug}")
async def view_newsletter(request: Request, slug: str):
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    is_super = _is_super_admin(request, email)
    if (newsletter.quarantined_at or newsletter.deleted_at) and not is_super:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    admin_senders = await storage.list_admin_senders(_db(request), email)
    base_url = str(request.base_url).rstrip("/")
    # Only needed to render the "Copy public link" block, which is admin-only -- an
    # unadministered sender's key is never put in front of a random logged-in user.
    sender_share_key = None
    if newsletter.from_email and (is_super or newsletter.from_email in admin_senders):
        settings = await storage.get_sender_settings(_db(request), newsletter.from_email)
        sender_share_key = settings.share_key if settings else None
    return _render(
        "permalink.html",
        newsletter=newsletter,
        identity_display=identity_display,
        identity_email=email,
        is_admin=newsletter.from_email in admin_senders,
        is_super_admin=is_super,
        base_url=base_url,
        canonical_url=f"{base_url}/n/{slug}",
        sender_share_key=sender_share_key,
    )


@app.get("/n/{slug}/images/{content_id}")
async def view_image(request: Request, slug: str, content_id: str):
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Image not found")

    image = await storage.get_image(_db(request), slug, content_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    content_type, data = image
    return Response(content=data, media_type=content_type)


@app.post("/n/{slug}/delete")
async def delete_newsletter(request: Request, slug: str):
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    if not await _can_administer(request, email, newsletter):
        raise HTTPException(status_code=403, detail="Not an admin for this newsletter's sender")

    await storage.delete_newsletter(_db(request), slug, deleted_by=email)
    await storage.log_action(_db(request), actor_email=email, action="newsletter.delete", target=slug, detail=newsletter.subject)
    return RedirectResponse(url="/archive", status_code=303)


@app.post("/n/{slug}/date")
async def update_newsletter_date(request: Request, slug: str):
    """Retroactively set a newsletter's send date -- for backfilling the archive with
    old issues under their original date. Same admin-of-sender-or-super-admin gate as
    delete."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    if not await _can_administer(request, email, newsletter):
        raise HTTPException(status_code=403, detail="Not an admin for this newsletter's sender")

    form = await _parse_form(request)
    try:
        parsed_date = datetime.strptime((form.get("received_at") or "").strip(), "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date, expected YYYY-MM-DD")

    received_at = parsed_date.replace(tzinfo=timezone.utc).isoformat()
    await storage.update_received_at(_db(request), slug, received_at)
    await storage.log_action(
        _db(request), actor_email=email, action="newsletter.date_update", target=slug,
        detail=f"received_at -> {received_at}",
    )
    return RedirectResponse(url=f"/n/{slug}", status_code=303)


@app.post("/n/{slug}/reprocess")
async def reprocess_newsletter(request: Request, slug: str):
    """Re-run the parse/resolve/sanitize pipeline against this newsletter's stored
    raw_eml and overwrite sanitized_html in place. Same admin gate as delete/date-edit."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    if not await _can_administer(request, email, newsletter):
        raise HTTPException(status_code=403, detail="Not an admin for this newsletter's sender")

    from worker_entry import reprocess_via_d1

    await reprocess_via_d1(slug, _db(request), _bucket(request))
    await storage.log_action(_db(request), actor_email=email, action="newsletter.reprocess", target=slug)
    return RedirectResponse(url=f"/n/{slug}", status_code=303)


@app.post("/n/{slug}/visibility")
async def update_newsletter_visibility(request: Request, slug: str):
    """Publish or unpublish a single newsletter. Private keeps it on the authenticated
    site but off every public surface -- embeds and share links. Same
    admin-of-sender-or-super-admin gate as delete/date-edit."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    if not await _can_administer(request, email, newsletter):
        raise HTTPException(status_code=403, detail="Not an admin for this newsletter's sender")

    form = await _parse_form(request)
    visibility = (form.get("visibility") or "").strip()
    if visibility not in ("public", "private"):
        raise HTTPException(status_code=400, detail="visibility must be public or private")

    await storage.set_newsletter_visibility(_db(request), slug, visibility)
    await storage.log_action(
        _db(request), actor_email=email, action="newsletter.visibility", target=slug,
        detail=f"{newsletter.visibility} -> {visibility}",
    )
    return RedirectResponse(url=f"/n/{slug}", status_code=303)


@app.post("/n/{slug}/share-key")
async def mint_share_key_from_newsletter(request: Request, slug: str):
    """Convenience entry point for "Create public link" on a newsletter page -- mints
    the sharing key for that newsletter's sender (see /permissions for the full
    per-sender controls). No-op if one already exists, so a double-click can't rotate
    the key and break links already shared."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None or not newsletter.from_email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    if not await _can_administer(request, email, newsletter):
        raise HTTPException(status_code=403, detail="Not an admin for this newsletter's sender")

    settings = await storage.get_sender_settings(_db(request), newsletter.from_email)
    if not (settings and settings.share_key):
        await storage.set_sender_share_key(
            _db(request), newsletter.from_email, secrets.token_urlsafe(16), email
        )
        await storage.log_action(
            _db(request), actor_email=email, action="sender.share_key_mint",
            target=newsletter.from_email, detail=f"via {slug}",
        )
    return RedirectResponse(url=f"/n/{slug}", status_code=303)


@app.get("/help")
async def help_page(request: Request):
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    return _render(
        "help.html",
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=_is_super_admin(request, email),
    )


@app.get("/about")
async def about_page(request: Request):
    """Attribution/credits -- the logo and "made by" line that used to sit in the footer
    on every page, moved here so the footer is just two buttons."""
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    return _render(
        "about.html",
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=_is_super_admin(request, email),
    )


@app.get("/admin")
async def admin_redirect():
    """Old combined admin console -- now split into /embeds (any user) and /permissions
    (read-only for everyone, manageable by the super admin). Kept as a redirect for
    anyone with the old URL bookmarked."""
    return RedirectResponse(url="/permissions", status_code=308)


@app.get("/permissions")
async def permissions_dashboard(request: Request):
    """Read-only for everyone except the super admin, who can add/revoke grants here.
    Visible to every authenticated user -- this is an internal tool, so who administers
    which sender isn't sensitive, and full transparency here beats hiding it."""
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    is_super = _is_super_admin(request, email)
    grants = await storage.list_admin_grants(_db(request))

    # Sender settings section: senders this user may configure (all of them for a super
    # admin). "inherited" spells out the resolve_default_visibility fallback so an admin
    # can see why new mail lands where it does without an explicit setting.
    senders = await storage.list_senders(_db(request))
    admin_senders = set(await storage.list_admin_senders(_db(request), email))
    settings_by_sender = {s.from_email: s for s in await storage.list_sender_settings(_db(request))}
    granted_senders = {g.from_email for g in grants}
    sender_settings = []
    for sender in senders:
        if not (is_super or sender.from_email in admin_senders):
            continue
        settings = settings_by_sender.get(sender.from_email)
        explicit = settings.default_visibility if settings else None
        sender_settings.append(
            {
                "from_email": sender.from_email,
                "name": sender.name,
                "default_visibility": explicit or ("public" if sender.from_email in granted_senders else "private"),
                "is_explicit": bool(explicit),
                "share_key": settings.share_key if settings else None,
            }
        )

    return _render(
        "permissions.html",
        grants=grants,
        sender_settings=sender_settings,
        base_url=str(request.base_url).rstrip("/"),
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=is_super,
    )


@app.post("/permissions/grants")
async def add_admin_grant(request: Request):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    grant_user_email = (form.get("user_email") or "").strip().lower()
    from_email = (form.get("from_email") or "").strip().lower()
    if grant_user_email and from_email:
        await storage.add_admin_grant(_db(request), grant_user_email, from_email)
        await storage.log_action(
            _db(request), actor_email=email, action="permission.grant", target=from_email,
            detail=f"granted to {grant_user_email}",
        )
    return RedirectResponse(url="/permissions", status_code=303)


@app.post("/permissions/grants/{grant_id}/delete")
async def delete_admin_grant(request: Request, grant_id: int):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    await storage.delete_admin_grant(_db(request), grant_id)
    await storage.log_action(_db(request), actor_email=email, action="permission.revoke", target=str(grant_id))
    return RedirectResponse(url="/permissions", status_code=303)


async def _can_administer_sender(request: Request, email: str, from_email: str) -> bool:
    """Sender-level counterpart to _can_administer (which needs a newsletter): may this
    user change settings for this sending address?"""
    if _is_super_admin(request, email):
        return True
    return from_email in await storage.list_admin_senders(_db(request), email)


@app.post("/permissions/senders/default")
async def set_sender_default(request: Request):
    """Default visibility for this sender's *incoming* newsletters. Set by one of that
    sender's admins (or the super admin); overrides the administered-sender fallback in
    storage.resolve_default_visibility."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    from_email = (form.get("from_email") or "").strip().lower()
    visibility = (form.get("visibility") or "").strip()
    if not from_email or visibility not in ("public", "private"):
        raise HTTPException(status_code=400, detail="from_email and a valid visibility are required")
    if not await _can_administer_sender(request, email, from_email):
        raise HTTPException(status_code=403, detail="Not an admin for this sender")

    await storage.set_sender_default_visibility(_db(request), from_email, visibility, email)
    await storage.log_action(
        _db(request), actor_email=email, action="sender.default_visibility",
        target=from_email, detail=visibility,
    )
    return RedirectResponse(url="/permissions", status_code=303)


@app.post("/permissions/senders/share-key")
async def set_sender_share_key(request: Request):
    """Mint or revoke this sender's public sharing key. Revoking instantly breaks every
    link shared under the old key, which is the point -- it's the kill switch."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    from_email = (form.get("from_email") or "").strip().lower()
    action = (form.get("action") or "").strip()
    if not from_email or action not in ("mint", "revoke"):
        raise HTTPException(status_code=400, detail="from_email and action=mint|revoke are required")
    if not await _can_administer_sender(request, email, from_email):
        raise HTTPException(status_code=403, detail="Not an admin for this sender")

    share_key = secrets.token_urlsafe(16) if action == "mint" else None
    await storage.set_sender_share_key(_db(request), from_email, share_key, email)
    await storage.log_action(
        _db(request), actor_email=email,
        action="sender.share_key_mint" if action == "mint" else "sender.share_key_revoke",
        target=from_email,
    )
    return RedirectResponse(url="/permissions", status_code=303)


@app.get("/quarantine")
async def quarantine_dashboard(request: Request):
    """Super-admin only: newsletters from senders outside *.asu.edu (and not on the
    allowlist) land here instead of the public archive/homepage/embeds. Nothing is
    rejected or silently dropped at ingest -- everything ends up visible either here or
    in the normal archive, so email-delivery problems stay diagnosable."""
    email, identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    quarantined = await storage.list_quarantined(_db(request))
    allowlist = await storage.list_allowlist(_db(request))
    return _render(
        "quarantine.html",
        quarantined=quarantined,
        allowlist=allowlist,
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=True,
    )


@app.post("/quarantine/{slug}/release")
async def release_quarantined(request: Request, slug: str):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    await storage.release_from_quarantine(_db(request), slug)
    await storage.log_action(_db(request), actor_email=email, action="quarantine.release", target=slug)
    return RedirectResponse(url="/quarantine", status_code=303)


@app.post("/quarantine/{slug}/whitelist")
async def whitelist_from_quarantine(request: Request, slug: str):
    """Whitelists this newsletter's sender and releases every quarantined newsletter
    already sitting there from that same sender in one action."""
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None or not newsletter.from_email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    await storage.add_to_allowlist(_db(request), newsletter.from_email)
    await storage.release_all_from_sender(_db(request), newsletter.from_email)
    await storage.log_action(
        _db(request), actor_email=email, action="quarantine.whitelist", target=newsletter.from_email,
        detail=f"via {slug}",
    )
    return RedirectResponse(url="/quarantine", status_code=303)


@app.post("/quarantine/allowlist")
async def add_allowlist_entry(request: Request):
    """Manually pre-authorize a non-ASU sender before they've sent anything yet."""
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    allow_email = (form.get("email") or "").strip().lower()
    if allow_email:
        await storage.add_to_allowlist(_db(request), allow_email)
        await storage.release_all_from_sender(_db(request), allow_email)
        await storage.log_action(_db(request), actor_email=email, action="quarantine.allowlist_add", target=allow_email)
    return RedirectResponse(url="/quarantine", status_code=303)


@app.post("/quarantine/allowlist/{entry_id}/delete")
async def delete_allowlist_entry(request: Request, entry_id: int):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    await storage.remove_from_allowlist(_db(request), entry_id)
    await storage.log_action(_db(request), actor_email=email, action="quarantine.allowlist_remove", target=str(entry_id))
    return RedirectResponse(url="/quarantine", status_code=303)


@app.get("/deleted")
async def deleted_dashboard(request: Request):
    """Super-admin only: soft-deleted newsletters -- nothing is ever actually erased by
    the delete action, just hidden and marked with who deleted it and when, so a mistake
    (increasingly possible now that more people have delete rights) can be undone here."""
    email, identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    deleted = await storage.list_deleted(_db(request))
    return _render(
        "deleted.html",
        deleted=deleted,
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=True,
    )


@app.post("/deleted/{slug}/restore")
async def restore_deleted(request: Request, slug: str):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    await storage.restore_newsletter(_db(request), slug)
    await storage.log_action(_db(request), actor_email=email, action="newsletter.restore", target=slug)
    return RedirectResponse(url="/deleted", status_code=303)


@app.get("/admin/newsletters")
async def admin_all_newsletters(request: Request, page: int = 1):
    """Super-admin only: every newsletter ever ingested, ordered by created_at (when it
    was actually added to the archive, not received_at/the email's own date), including
    quarantined and deleted ones with their status shown -- a true ingestion log."""
    email, identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    page = max(page, 1)
    total = await storage.count_all_newsletters(_db(request))
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(page, total_pages)
    newsletters = await storage.list_all_newsletters(
        _db(request), limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE
    )
    return _render(
        "admin_newsletters.html",
        newsletters=newsletters,
        page=page,
        total_pages=total_pages,
        pagination_items=_pagination_items(page, total_pages),
        has_next=page < total_pages,
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=True,
    )


@app.get("/admin/actions")
async def admin_action_log(request: Request, page: int = 1):
    """Super-admin only: audit trail -- a "login" row at most once per user per rolling
    4-hour session (see storage.log_login_if_new_session, hooked into _current_user),
    plus one row for every modification action (delete/restore/backdate/reprocess a
    newsletter, create/edit/revoke an embed, grant/revoke permissions, every quarantine
    action) -- each recording who did it."""
    email, identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    page = max(page, 1)
    total = await storage.count_action_log(_db(request))
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(page, total_pages)
    entries = await storage.list_action_log(_db(request), limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE)
    return _render(
        "admin_actions.html",
        entries=entries,
        page=page,
        total_pages=total_pages,
        pagination_items=_pagination_items(page, total_pages),
        has_next=page < total_pages,
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=True,
    )


@app.post("/maintenance/backfill")
async def maintenance_backfill(request: Request):
    """Bulk-runs the same pipeline as a newsletter's own "Reprocess links" (resolving
    tracked links, mirroring external images to R2, picking a thumbnail) across a
    handful of not-yet-backfilled newsletters at a time. No admin UI trigger any more --
    the initial backfill finished -- but kept for any future one-off need, driven from a
    local loop and gated by the BACKFILL_MAINTENANCE_TOKEN secret (X-Maintenance-Token
    header) instead of an Access session, so it never touches your personal
    identity/session at all. Returns the batch result as JSON directly rather than a
    redirect."""
    token = _env_var(request, "BACKFILL_MAINTENANCE_TOKEN")
    if not token or request.headers.get("X-Maintenance-Token") != token:
        raise HTTPException(status_code=401, detail="Invalid or missing maintenance token")

    from worker_entry import backfill_batch

    return await backfill_batch(_db(request), _bucket(request))


@app.post("/maintenance/reprocess/{slug}")
async def maintenance_reprocess(request: Request, slug: str):
    """Script-friendly single-newsletter twin of /maintenance/backfill -- re-runs the
    parse/resolve/sanitize pipeline against one already-archived newsletter's stored
    raw_eml, for testing a sanitizer change against one newsletter before running it
    across everything. Same BACKFILL_MAINTENANCE_TOKEN gate, same reasoning: never
    touches an Access session."""
    token = _env_var(request, "BACKFILL_MAINTENANCE_TOKEN")
    if not token or request.headers.get("X-Maintenance-Token") != token:
        raise HTTPException(status_code=401, detail="Invalid or missing maintenance token")

    from worker_entry import reprocess_via_d1

    newsletter, fully_processed = await reprocess_via_d1(slug, _db(request), _bucket(request))
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    return {"slug": newsletter.slug, "subject": newsletter.subject, "fully_processed": fully_processed}


@app.get("/embeds")
async def embeds_dashboard(request: Request, edit: str | None = None):
    """Any authenticated user can reach this page to create/manage their own embeds --
    embed creation isn't an admin-gated action (see create_embed). The table only lists
    embeds this user is allowed to manage: everything for a super admin, or (their own +
    their administered senders') for everyone else.

    ?edit={token} pre-fills the embed form for editing that embed in place (same token,
    same URL, so any iframe already using it keeps working) instead of creating a new one.
    """
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    is_super = _is_super_admin(request, email)
    admin_senders = await storage.list_admin_senders(_db(request), email)

    all_embeds = await storage.list_embed_queries(_db(request))
    embeds = (
        all_embeds
        if is_super
        else [e for e in all_embeds if e.sender_email in admin_senders or e.created_by == email]
    )

    edit_embed = None
    if edit:
        edit_embed = await storage.get_embed_query(_db(request), edit)
        if edit_embed is not None and not await _can_manage_embed(request, email, edit_embed):
            raise HTTPException(status_code=403, detail="Not allowed to edit this embed")

    return _render(
        "embeds.html",
        embeds=embeds,
        edit_embed=edit_embed,
        base_url=str(request.base_url).rstrip("/"),
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=is_super,
    )


async def _can_manage_embed(request: Request, email: str, embed: storage.EmbedQuery) -> bool:
    """True if `email` may edit/delete this embed: whoever created it, an admin of its
    sender, or the super admin. Creating an embed in the first place is open to any
    authenticated user regardless of sender -- an embed only ever surfaces newsletters
    that user could already view -- so this narrower check only guards against a
    stranger editing/revoking someone else's already-published, possibly-live-embedded
    widget."""
    if _is_super_admin(request, email):
        return True
    if embed.created_by == email:
        return True
    if embed.sender_email:
        admin_senders = await storage.list_admin_senders(_db(request), email)
        if embed.sender_email in admin_senders:
            return True
    return False


def _parse_embed_form(form: dict[str, str]) -> tuple[str, str | None, int, str, bool]:
    """(name, sender_email, result_limit, sort, show_thumbnails) from a create/edit
    embed form submission. An unchecked HTML checkbox submits no field at all, so its
    absence (not just "off") means False -- this is what makes every existing embed's
    show_thumbnails default to off."""
    name = (form.get("name") or "").strip()
    sender_email = (form.get("sender_email") or "").strip().lower() or None
    sort = "oldest" if form.get("sort") == "oldest" else "newest"
    try:
        result_limit = int(form.get("result_limit") or 5)
    except ValueError:
        result_limit = 5
    if result_limit != 0:
        result_limit = max(1, min(50, result_limit))  # 0 is the deliberate "show all" sentinel
    show_thumbnails = form.get("show_thumbnails") == "on"
    return name, sender_email, result_limit, sort, show_thumbnails


@app.post("/embeds")
async def create_embed(request: Request):
    """Any authenticated user can create an embed for any sender (or all senders) --
    an embed only ever exposes newsletters that user could already view on the site, so
    this isn't a meaningful new grant of access. Editing/revoking someone else's embed
    is the narrower, admin-gated action -- see _can_manage_embed."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    name, sender_email, result_limit, sort, show_thumbnails = _parse_embed_form(form)

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    token = secrets.token_urlsafe(16)
    await storage.create_embed_query(
        _db(request),
        token=token,
        name=name,
        sender_email=sender_email,
        result_limit=result_limit,
        sort=sort,
        created_by=email,
        show_thumbnails=show_thumbnails,
    )
    await storage.log_action(_db(request), actor_email=email, action="embed.create", target=token, detail=name)
    return RedirectResponse(url="/embeds", status_code=303)


@app.post("/embeds/{token}/edit")
async def edit_embed(request: Request, token: str):
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    if not await _can_manage_embed(request, email, embed):
        raise HTTPException(status_code=403, detail="Not allowed to edit this embed")

    form = await _parse_form(request)
    name, sender_email, result_limit, sort, show_thumbnails = _parse_embed_form(form)

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    await storage.update_embed_query(
        _db(request),
        token,
        name=name,
        sender_email=sender_email,
        result_limit=result_limit,
        sort=sort,
        show_thumbnails=show_thumbnails,
    )
    await storage.log_action(_db(request), actor_email=email, action="embed.edit", target=token, detail=name)
    return RedirectResponse(url="/embeds", status_code=303)


@app.post("/embeds/{token}/delete")
async def delete_embed(request: Request, token: str):
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    if not await _can_manage_embed(request, email, embed):
        raise HTTPException(status_code=403, detail="Not allowed to revoke this embed")
    await storage.delete_embed_query(_db(request), token)
    await storage.log_action(_db(request), actor_email=email, action="embed.revoke", target=token, detail=embed.name)
    return RedirectResponse(url="/embeds", status_code=303)


@app.get("/embed/{token}")
async def embed_list(request: Request, token: str):
    """Public, unauthenticated -- deliberately doesn't call _current_user. Exists behind
    a Cloudflare Access Bypass policy scoped to /embed/*; see the plan for why this is
    safe (the token is the security boundary, re-validated server-side, not Access)."""
    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    newsletters = await storage.list_newsletters(
        _db(request),
        sender=embed.sender_email,
        sort=embed.sort,
        limit=embed.result_limit,
        public_only=True,
    )

    # Prefer the sender's display name (e.g. "Veterans at ASU") over their bare address,
    # same as the "Show previous newsletters from X" button on the permalink page.
    sender_name = None
    if embed.sender_email:
        sender_name = embed.sender_email
        if newsletters:
            sender_name = parseaddr(newsletters[0].from_address)[0] or sender_name

    return _render(
        "embed_list.html",
        feed_title=embed.name,
        show_thumbnails=embed.show_thumbnails,
        item_url_prefix=f"/embed/{token}",
        newsletters=newsletters,
        sender_name=sender_name,
        standalone=not _looks_embedded(request),
    )


@app.get("/embed/{token}/n/{slug}")
async def embed_permalink(request: Request, token: str, slug: str):
    """Public, unauthenticated. Re-validates the newsletter actually matches this
    embed's saved sender filter on every request -- the slug alone isn't enough to view
    it, so this can't be used to read an unrelated newsletter for free."""
    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None or newsletter.quarantined_at or newsletter.deleted_at:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    if newsletter.visibility != "public":
        raise HTTPException(status_code=404, detail="Newsletter not found")
    if embed.sender_email and newsletter.from_email != embed.sender_email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    # Prefer the sender's display name (e.g. "Veterans at ASU") over their bare address --
    # this newsletter's from_address is guaranteed to match embed.sender_email above.
    sender_name = parseaddr(newsletter.from_address)[0] or embed.sender_email
    embed_back_label = (
        f"Show previous newsletters from {sender_name}" if embed.sender_email else "Show previous newsletters"
    )
    base_url = str(request.base_url).rstrip("/")
    return _render(
        "permalink.html",
        newsletter=newsletter,
        identity_display=None,
        identity_email=None,
        is_admin=False,
        is_super_admin=False,
        embed_back_url=f"/embed/{token}",
        embed_back_label=embed_back_label,
        base_url=base_url,
        canonical_url=f"{base_url}/embed/{token}/n/{slug}",
    )


@app.get("/embed/s/{key}")
async def sender_feed(request: Request, key: str):
    """Public, unauthenticated: a sender's public newsletters, reached by an unguessable
    key one of that sender's admins minted (storage.set_sender_share_key). Deliberately
    not an embed_queries row -- it needs no configuration and shouldn't clutter /embeds.
    Lives under /embed/* so the existing Access Bypass policy covers it.

    Two path segments, so it can't collide with /embed/{token} (one) or
    /embed/{token}/n/{slug} (three)."""
    from_email = await storage.get_sender_by_share_key(_db(request), key)
    if not from_email:
        raise HTTPException(status_code=404, detail="Not found")

    newsletters = await storage.list_newsletters(
        _db(request), sender=from_email, sort="newest", limit=_PAGE_SIZE, public_only=True
    )
    # Nothing public for this sender -- 404 rather than an empty page, so a live key
    # never confirms anything about a sender who has published nothing.
    if not newsletters:
        raise HTTPException(status_code=404, detail="Not found")

    sender_name = parseaddr(newsletters[0].from_address)[0] or from_email
    return _render(
        "embed_list.html",
        feed_title=sender_name,
        show_thumbnails=True,
        item_url_prefix=f"/embed/s/{key}",
        newsletters=newsletters,
        sender_name=sender_name,
        standalone=not _looks_embedded(request),
    )


@app.get("/embed/s/{key}/n/{slug}")
async def sender_share_permalink(request: Request, key: str, slug: str):
    """Public, unauthenticated: the shareable single-newsletter link. Same re-validation
    shape as embed_permalink -- the key resolves to a sender and the newsletter must
    actually belong to it and be public, so a key can't be used to read someone else's
    or an unpublished newsletter."""
    from_email = await storage.get_sender_by_share_key(_db(request), key)
    if not from_email:
        raise HTTPException(status_code=404, detail="Not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None or newsletter.quarantined_at or newsletter.deleted_at:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    if newsletter.visibility != "public" or newsletter.from_email != from_email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    sender_name = parseaddr(newsletter.from_address)[0] or from_email
    base_url = str(request.base_url).rstrip("/")
    return _render(
        "permalink.html",
        newsletter=newsletter,
        identity_display=None,
        identity_email=None,
        is_admin=False,
        is_super_admin=False,
        embed_back_url=f"/embed/s/{key}",
        embed_back_label=f"Show previous newsletters from {sender_name}",
        base_url=base_url,
        canonical_url=f"{base_url}/embed/s/{key}/n/{slug}",
    )


@app.post("/ingest")
async def http_ingest(request: Request, to: str):
    """Push-based / manual ingestion -- same shared-secret gate as before, now calling
    the D1-backed orchestration in worker_entry.py (the counterpart to ingest.ingest()).
    Set NEWSLETTER_ARCHIVE_INGEST_TOKEN (wrangler secret put) before exposing this to
    anything outside your own testing -- unset, this route has no protection at all."""
    ingest_token = _env_var(request, "NEWSLETTER_ARCHIVE_INGEST_TOKEN")
    if ingest_token and request.headers.get("X-Ingest-Token") != ingest_token:
        raise HTTPException(status_code=401, detail="Invalid or missing ingest token")

    raw_bytes = await request.body()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")

    from worker_entry import ingest_via_d1

    newsletter = await ingest_via_d1(raw_bytes, to, _db(request), _bucket(request))
    return {"slug": newsletter.slug, "subject": newsletter.subject}
