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
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs

import jinja2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import access
from .. import storage_d1 as storage
from .assets import FAVICON_ICO_BASE64, LOGO_PNG_BASE64

app = FastAPI(title="Newsletter Archive")

# Set this before exposing /ingest to anything outside your own testing -- checked
# against the X-Ingest-Token header on POST /ingest.
INGEST_TOKEN = os.environ.get("NEWSLETTER_ARCHIVE_INGEST_TOKEN")

_PAGE_SIZE = 25

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

.site-header {
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
}
.site-header-inner {
  max-width: 900px; margin: 0 auto; padding: 0.9rem 2rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
}
.site-header-inner.wide { max-width: 1150px; }
.site-brand { display: flex; align-items: center; text-decoration: none; }
.site-titles { display: flex; flex-direction: column; line-height: 1.25; }
.site-title { color: var(--asu-maroon); font-weight: 700; font-size: 1.1rem; }
.site-subtitle { color: var(--text-muted); font-size: 0.75rem; }
.header-right { display: flex; align-items: center; gap: 1rem; }
.admin-link {
  color: var(--asu-maroon); font-size: 0.85rem; font-weight: 600; text-decoration: none;
  border: 1px solid var(--asu-maroon); border-radius: 8px; padding: 0.3rem 0.7rem;
}
.admin-link:hover { background: var(--asu-gold); border-color: var(--asu-gold); }
.identity { text-align: right; font-size: 0.85rem; line-height: 1.35; }
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
  .layout { flex-direction: column; }
  .sidebar { width: 100%; }
}

.newsletter-list { list-style: none; padding: 0; margin: 0; }
.newsletter-list li { padding: 0.9rem 0; border-bottom: 1px solid var(--border); }
.newsletter-list a.subject { color: var(--text-primary); font-weight: 600; text-decoration: none; }
.newsletter-list a.subject:hover { color: var(--asu-maroon); }

.meta { color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem; }

.pagination { display: flex; justify-content: space-between; margin-top: 1.5rem; }
.pagination a { color: var(--asu-maroon); font-weight: 600; text-decoration: none; }
.pagination a:hover { text-decoration: underline; }

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

.admin-form { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0 1.5rem; }
.admin-form input, .admin-form select {
  padding: 0.6rem 0.8rem; border: 1px solid var(--border); border-radius: 8px;
  background: rgba(255, 255, 255, 0.9); color: var(--text-primary); flex: 1; min-width: 200px; font-size: 1rem;
}
.admin-form input[type="number"] { flex: 0 0 auto; min-width: 0; width: 5rem; }
.admin-form select { flex: 0 0 auto; min-width: 0; width: auto; }
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
.embed-snippet {
  display: block; font-size: 0.72rem; background: var(--asu-sand); color: var(--text-primary);
  padding: 0.3rem 0.5rem; border-radius: 6px; margin-bottom: 0.35rem; word-break: break-all;
}
.copy-btn { font-size: 0.78rem; padding: 0.3rem 0.6rem; }
.cancel-link { align-self: center; color: var(--text-muted); font-size: 0.85rem; text-decoration: underline; }
.edit-link {
  color: var(--asu-maroon); font-size: 0.8rem; font-weight: 600; text-decoration: none;
  margin-right: 0.75rem;
}
.edit-link:hover { text-decoration: underline; }

.permalink-meta { margin-bottom: 1rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
.permalink-meta h1 {
  margin: 0 0 0.5rem; font-size: 1.4rem;
  color: var(--asu-black); background: var(--asu-gold); padding: 0.2rem 0.5rem; display: inline-block;
}
.permalink-meta .meta { font-size: 0.9rem; }

#body-frame { width: 100%; border: 0; display: block; min-height: 400px; }
.empty { color: var(--text-muted); padding: 2rem 0; }

.app-footer {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 0.3rem; font-size: 0.85rem; color: rgba(74, 74, 88, 0.8);
  text-align: center; padding: 1.5rem 1rem 2rem;
}
.app-footer p { margin: 0; padding: 0; }
.app-footer a { color: rgba(140, 29, 64, 0.85); font-weight: 600; text-decoration: none; }
.app-footer a:hover { text-decoration: underline; }
.app-footer__logo { display: block; height: 60px; width: auto; border-radius: 6px; margin-bottom: 0.3rem; }
"""

_TEMPLATES = {
    "base.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Newsletter Archive{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="icon" href="/favicon.ico">
</head>
<body>
  <header class="site-header">
    <div class="site-header-inner {{ self.main_class() }}">
      <a class="site-brand" href="/">
        <span class="site-titles">
          <span class="site-title">Newsletter Archive</span>
          <span class="site-subtitle">Center for Evolution and Medicine</span>
        </span>
      </a>
      <div class="header-right">
        {% if identity_display %}<a class="admin-link" href="/help">Help</a>{% endif %}
        {% if is_super_admin %}<a class="admin-link" href="/admin">Admin</a>{% endif %}
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
    <a href="https://evmed.asu.edu"><img src="/static/logo.png" alt="Center for Evolution and Medicine logo" class="app-footer__logo" width="220" height="55"></a>
    <p>Made by the <a href="https://evmed.asu.edu/">Center for Evolution and Medicine</a> at <a href="https://asu.edu">Arizona State University</a>.</p>
    <p>For any issues contact <a href="mailto:suhail.ghafoor@asu.edu">suhail.ghafoor@asu.edu</a>.</p>
  </footer>
</body>
</html>
""",
    "list.html": """{% extends "base.html" %}
{% block title %}Newsletter Archive{% endblock %}
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
              <a class="subject" href="/n/{{ n.slug }}">{{ n.subject }}</a>
              <div class="meta">
                {{ n.from_address }} &middot; {{ (n.received_at or n.created_at)|humandate }}
                {% if is_super_admin or n.from_email in admin_senders %}
                  &middot;
                  <form class="delete-form" method="post" action="/n/{{ n.slug }}/delete" onsubmit="return confirm('Delete this newsletter?');">
                    <button type="submit" class="delete-btn">Delete</button>
                  </form>
                {% endif %}
              </div>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="empty">No newsletters archived yet.</p>
      {% endif %}

      <div class="pagination">
        {% if page > 1 %}
          <a href="?sender={{ filters.sender }}&sort={{ filters.sort }}&page={{ page - 1 }}">&larr; Newer</a>
        {% else %}<span></span>{% endif %}
        {% if has_next %}
          <a href="?sender={{ filters.sender }}&sort={{ filters.sort }}&page={{ page + 1 }}">Older &rarr;</a>
        {% endif %}
      </div>
    </div>
  </div>
{% endblock %}
""",
    "permalink.html": """{% extends "base.html" %}
{% block title %}{{ newsletter.subject }} · Newsletter Archive{% endblock %}
{% block main_class %}wide{% endblock %}
{% block content %}
  <div class="layout">
    <div class="content">
      <div class="permalink-meta">
        <h1>{{ newsletter.subject }}</h1>
        <div class="meta">From {{ newsletter.from_address }} &middot; {{ (newsletter.received_at or newsletter.created_at)|humandate }} &middot; sent to {{ newsletter.to_address }}</div>
      </div>

      {% if newsletter.sanitized_html %}
        <iframe id="body-frame" sandbox="allow-same-origin" srcdoc="{{ newsletter.sanitized_html }}"></iframe>
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
          <h2>Admin</h2>
          <form class="date-form" method="post" action="/n/{{ newsletter.slug }}/date">
            <label for="received_at">Send date</label>
            <input type="date" id="received_at" name="received_at" value="{{ (newsletter.received_at or newsletter.created_at)[:10] }}">
            <button type="submit">Update date</button>
          </form>
          <form class="secondary-form" method="post" action="/n/{{ newsletter.slug }}/reprocess" title="Re-run link resolution and unsubscribe-link cleanup against the original email">
            <button type="submit" class="secondary-btn">Reprocess links</button>
          </form>
          <form class="delete-form" method="post" action="/n/{{ newsletter.slug }}/delete" onsubmit="return confirm('Delete this newsletter?');">
            <button type="submit" class="delete-btn">Delete this newsletter</button>
          </form>
        </div>
      </aside>
    {% endif %}
  </div>
{% endblock %}
""",
    "help.html": """{% extends "base.html" %}
{% block title %}Help · Newsletter Archive{% endblock %}
{% block content %}
  <h1>Help</h1>

  <h2>Get your newsletter into the archive</h2>
  <p class="meta">Send (or CC/BCC) your newsletter to <strong>newsletters@evmed.app</strong>
  when you send it out. It shows up on the <a href="/">homepage</a> automatically within
  moments -- no extra steps. Unsubscribe / manage-preferences links are automatically
  disabled in the archived copy (so the public archive can't be used to unsubscribe
  someone); everything else, including all your regular content links, is preserved
  exactly as sent.</p>

  <h2>Add a "recent newsletters" widget to your department website</h2>
  <p class="meta">Any of the newsletters in the archive can be embedded as a small,
  public widget on another website -- no login required for people viewing it, and
  clicking a newsletter opens the full thing. Any logged-in archive user can create one,
  no admin access needed:</p>
  <ol class="meta">
    <li>Go to <a href="/admin">/admin</a>.</li>
    <li>Under <strong>Embeds</strong>, fill in a name, a sender email to filter to
    (leave blank to show newsletters from <em>all</em> senders), how many to show (0
    shows all of them), and sort order, then <strong>Create embed</strong>.</li>
    <li>Click <strong>Copy iframe code</strong> next to it and paste that directly into
    your website's HTML.</li>
  </ol>
  <p class="meta">You can revisit <a href="/admin">/admin</a> any time to
  <strong>Edit</strong> an embed you created (updates what it shows without breaking the
  link you already pasted somewhere) or <strong>Revoke</strong> it (immediately stops it
  from working anywhere it's embedded). Editing or revoking an embed someone else
  created requires admin access for that embed's sender.</p>

  <h2>Don't have admin access yet?</h2>
  <p class="meta">Admin access is per sending address -- it lets you delete newsletters
  from that sender in the archive, backdate them (useful for backfilling old issues),
  and edit/revoke embeds scoped to that sender even if you didn't create them. It's not
  needed just to create your own embeds.</p>
  <p class="meta">Contact Suhail
  (<a href="mailto:suhail.ghafoor@asu.edu">suhail.ghafoor@asu.edu</a>) to be set up as an
  admin for your sending address.</p>
{% endblock %}
""",
    "admin.html": """{% extends "base.html" %}
{% block title %}Admin · Newsletter Archive{% endblock %}
{% block content %}
  <h1>Admin</h1>

  {% if is_super_admin %}
    <h2>Grants</h2>
    <p class="meta">Grant a user admin rights over a sender: they'll be able to delete
    newsletters *from* that sender address, backdate them, and edit/revoke embeds scoped
    to it even if someone else created them. Every authenticated user can already view
    everything, and can already create their own embeds for any sender, regardless of
    grants.</p>

    <form class="admin-form" method="post" action="/admin/grants">
      <input type="email" name="user_email" placeholder="user@example.com" required>
      <input type="text" name="from_email" placeholder="news@sender.com" required>
      <button type="submit">Grant</button>
    </form>

    {% if grants %}
      <table class="admin-table">
        <thead><tr><th>User</th><th>Sender (from)</th><th></th></tr></thead>
        <tbody>
          {% for g in grants %}
            <tr>
              <td>{{ g.user_email }}</td>
              <td>{{ g.from_email }}</td>
              <td>
                <form class="delete-form" method="post" action="/admin/grants/{{ g.id }}/delete" onsubmit="return confirm('Revoke this grant?');">
                  <button type="submit" class="delete-btn">Revoke</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="empty">No admin grants yet.</p>
    {% endif %}
  {% endif %}

  <h2>Embeds</h2>
  <p class="meta">Publish a public, unauthenticated list of recent newsletters (optionally
  filtered by sender) for embedding as an iframe on a department website -- no login
  required to view it or open a newsletter from it. Revoke to break every iframe using it
  immediately; edit to change what an existing embed shows without breaking its URL.</p>

  <form class="admin-form" method="post" action="{{ ('/admin/embeds/' ~ edit_embed.token ~ '/edit') if edit_embed else '/admin/embeds' }}">
    <input type="text" name="name" placeholder="Name, e.g. CEM homepage widget" value="{{ edit_embed.name if edit_embed else '' }}" required>
    <input type="text" name="sender_email" placeholder="Sender email (blank = all senders)" value="{{ edit_embed.sender_email or '' if edit_embed else '' }}">
    <input type="number" name="result_limit" value="{{ edit_embed.result_limit if edit_embed else 5 }}" min="0" max="50" title="0 shows all newsletters">
    <select name="sort">
      <option value="newest" {{ "selected" if edit_embed and edit_embed.sort != "oldest" }}>Newest first</option>
      <option value="oldest" {{ "selected" if edit_embed and edit_embed.sort == "oldest" }}>Oldest first</option>
    </select>
    <button type="submit">{{ "Save changes" if edit_embed else "Create embed" }}</button>
    {% if edit_embed %}<a href="/admin" class="cancel-link">Cancel</a>{% endif %}
  </form>
  <p class="meta" style="margin-top: -0.75rem;">Limit: 0 shows every matching newsletter.</p>

  {% if embeds %}
    <table class="admin-table">
      <thead><tr><th>Name</th><th>Sender</th><th>Shows</th><th>Embed code</th><th></th></tr></thead>
      <tbody>
        {% for e in embeds %}
          <tr>
            <td>{{ e.name }}</td>
            <td>{{ e.sender_email or "All senders" }}</td>
            <td>{{ "All" if e.result_limit == 0 else "Last " ~ e.result_limit }}, {{ "oldest" if e.sort == "oldest" else "newest" }} first</td>
            <td>
              <code class="embed-snippet">{{ base_url }}/embed/{{ e.token }}</code>
              <button type="button" class="secondary-btn copy-btn" data-url="{{ base_url }}/embed/{{ e.token }}">Copy iframe code</button>
            </td>
            <td>
              <a href="/admin?edit={{ e.token }}" class="edit-link">Edit</a>
              <form class="delete-form" method="post" action="/admin/embeds/{{ e.token }}/delete" onsubmit="return confirm('Revoke this embed? Any iframe using it will break immediately.');">
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
        const snippet = `<iframe src="${btn.dataset.url}" width="100%" height="400" style="border:0;"></iframe>`;
        navigator.clipboard.writeText(snippet).then(() => {
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
  <title>{{ embed.name }}</title>
  <link rel="stylesheet" href="/static/style.css">
  <style>
    body { margin: 0; padding: 0.75rem 1rem; }
    .embed-list { list-style: none; margin: 0; padding: 0; }
    .embed-list li { padding: 0.6rem 0; border-bottom: 1px solid var(--border); }
    .embed-list li:last-child { border-bottom: none; }
    .embed-date { color: var(--asu-maroon); font-weight: 700; font-size: 0.9rem; margin-bottom: 0.15rem; }
    .embed-list a { color: var(--text-primary); font-weight: 600; text-decoration: none; font-size: 0.95rem; }
    .embed-list a:hover { color: var(--asu-maroon); }
  </style>
</head>
<body>
  {% if newsletters %}
    <ul class="embed-list">
      {% for n in newsletters %}
        <li>
          <div class="embed-date">{{ (n.received_at or n.created_at)|humandate }}</div>
          <a href="/embed/{{ token }}/n/{{ n.slug }}" target="_blank" rel="noopener">{{ n.subject }}</a>
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


_jinja_env = jinja2.Environment(loader=jinja2.DictLoader(_TEMPLATES), autoescape=True)
_jinja_env.filters["humandate"] = _humandate


def _render(template_name: str, **context) -> HTMLResponse:
    html = _jinja_env.get_template(template_name).render(**context)
    return HTMLResponse(html)


def _db(request: Request):
    return request.scope["env"].DB


async def _current_user(request: Request) -> tuple[str | None, str | None]:
    """(email, display_name) for the current request's Cloudflare Access identity.

    Identity comes from Cloudflare Access (see access.py) -- not from a client-supplied
    header. Every authenticated user can view every newsletter (Access already gates who
    reaches the site at all); a missing identity is the only thing that fails closed
    below, guarding the case where this Worker is reached some other way (e.g. its
    workers.dev URL) that Access doesn't protect.
    """
    identity = await access.get_identity(request)
    return access.identity_email(identity), access.display_name(identity)


def _is_super_admin(request: Request, email: str | None) -> bool:
    if not email:
        return False
    raw = getattr(request.scope["env"], "SUPER_ADMIN_EMAILS", "") or ""
    allowed = {addr.strip().lower() for addr in raw.split(",") if addr.strip()}
    return email.lower() in allowed


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


@app.get("/static/logo.png")
async def logo_png():
    return Response(content=base64.b64decode(LOGO_PNG_BASE64), media_type="image/png")


@app.get("/favicon.ico")
async def favicon_ico():
    return Response(content=base64.b64decode(FAVICON_ICO_BASE64), media_type="image/x-icon")


@app.get("/")
async def list_newsletters(
    request: Request,
    sender: str | None = None,
    sort: str = "newest",
    page: int = 1,
):
    page = max(page, 1)
    sort = "oldest" if sort == "oldest" else "newest"
    offset = (page - 1) * _PAGE_SIZE
    email, identity_display = await _current_user(request)
    if not email:
        return _render(
            "list.html",
            newsletters=[],
            senders=[],
            filters={"sender": sender or "", "sort": sort},
            page=page,
            has_next=False,
            identity_display=None,
            identity_email=None,
            admin_senders=set(),
            is_super_admin=False,
        )

    rows = await storage.list_newsletters(
        _db(request),
        sender=sender,
        sort=sort,
        limit=_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(rows) > _PAGE_SIZE
    senders = await storage.list_senders(_db(request))
    admin_senders = set(await storage.list_admin_senders(_db(request), email))
    return _render(
        "list.html",
        newsletters=rows[:_PAGE_SIZE],
        senders=senders,
        filters={"sender": sender or "", "sort": sort},
        page=page,
        has_next=has_next,
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

    admin_senders = await storage.list_admin_senders(_db(request), email)
    return _render(
        "permalink.html",
        newsletter=newsletter,
        identity_display=identity_display,
        identity_email=email,
        is_admin=newsletter.from_email in admin_senders,
        is_super_admin=_is_super_admin(request, email),
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

    await storage.delete_newsletter(_db(request), slug)
    return RedirectResponse(url="/", status_code=303)


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

    await reprocess_via_d1(slug, _db(request))
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


@app.get("/admin")
async def admin_dashboard(request: Request, edit: str | None = None):
    """Any authenticated user can reach this page to create/manage their own embeds --
    embed creation isn't an admin-gated action (see create_embed). Grants stay super-admin
    only (granting admin rights out to others is a higher-trust action), and the embeds
    table only lists ones this user is allowed to manage: everything for a super admin,
    or (their own + their administered senders') for everyone else.

    ?edit={token} pre-fills the embed form for editing that embed in place (same token,
    same URL, so any iframe already using it keeps working) instead of creating a new one.
    """
    email, identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    is_super = _is_super_admin(request, email)
    admin_senders = await storage.list_admin_senders(_db(request), email)

    grants = await storage.list_admin_grants(_db(request)) if is_super else []
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
        "admin.html",
        grants=grants,
        embeds=embeds,
        edit_embed=edit_embed,
        base_url=str(request.base_url).rstrip("/"),
        identity_display=identity_display,
        identity_email=email,
        is_super_admin=is_super,
    )


@app.post("/admin/grants")
async def add_admin_grant(request: Request):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    grant_user_email = (form.get("user_email") or "").strip().lower()
    from_email = (form.get("from_email") or "").strip().lower()
    if grant_user_email and from_email:
        await storage.add_admin_grant(_db(request), grant_user_email, from_email)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/grants/{grant_id}/delete")
async def delete_admin_grant(request: Request, grant_id: int):
    email, _identity_display = await _current_user(request)
    if not email or not _is_super_admin(request, email):
        raise HTTPException(status_code=404, detail="Not found")

    await storage.delete_admin_grant(_db(request), grant_id)
    return RedirectResponse(url="/admin", status_code=303)


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


def _parse_embed_form(form: dict[str, str]) -> tuple[str, str | None, int, str]:
    """(name, sender_email, result_limit, sort) from a create/edit embed form submission."""
    name = (form.get("name") or "").strip()
    sender_email = (form.get("sender_email") or "").strip().lower() or None
    sort = "oldest" if form.get("sort") == "oldest" else "newest"
    try:
        result_limit = int(form.get("result_limit") or 5)
    except ValueError:
        result_limit = 5
    if result_limit != 0:
        result_limit = max(1, min(50, result_limit))  # 0 is the deliberate "show all" sentinel
    return name, sender_email, result_limit, sort


@app.post("/admin/embeds")
async def create_embed(request: Request):
    """Any authenticated user can create an embed for any sender (or all senders) --
    an embed only ever exposes newsletters that user could already view on the site, so
    this isn't a meaningful new grant of access. Editing/revoking someone else's embed
    is the narrower, admin-gated action -- see _can_manage_embed."""
    email, _identity_display = await _current_user(request)
    if not email:
        raise HTTPException(status_code=404, detail="Not found")

    form = await _parse_form(request)
    name, sender_email, result_limit, sort = _parse_embed_form(form)

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    await storage.create_embed_query(
        _db(request),
        token=secrets.token_urlsafe(16),
        name=name,
        sender_email=sender_email,
        result_limit=result_limit,
        sort=sort,
        created_by=email,
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/embeds/{token}/edit")
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
    name, sender_email, result_limit, sort = _parse_embed_form(form)

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    await storage.update_embed_query(
        _db(request), token, name=name, sender_email=sender_email, result_limit=result_limit, sort=sort
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/embeds/{token}/delete")
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
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/embed/{token}")
async def embed_list(request: Request, token: str):
    """Public, unauthenticated -- deliberately doesn't call _current_user. Exists behind
    a Cloudflare Access Bypass policy scoped to /embed/*; see the plan for why this is
    safe (the token is the security boundary, re-validated server-side, not Access)."""
    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    newsletters = await storage.list_newsletters(
        _db(request), sender=embed.sender_email, sort=embed.sort, limit=embed.result_limit
    )
    return _render("embed_list.html", token=token, embed=embed, newsletters=newsletters)


@app.get("/embed/{token}/n/{slug}")
async def embed_permalink(request: Request, token: str, slug: str):
    """Public, unauthenticated. Re-validates the newsletter actually matches this
    embed's saved sender filter on every request -- the slug alone isn't enough to view
    it, so this can't be used to read an unrelated newsletter for free."""
    embed = await storage.get_embed_query(_db(request), token)
    if embed is None:
        raise HTTPException(status_code=404, detail="Not found")

    newsletter = await storage.get_by_slug(_db(request), slug)
    if newsletter is None:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    if embed.sender_email and newsletter.from_email != embed.sender_email:
        raise HTTPException(status_code=404, detail="Newsletter not found")

    return _render(
        "permalink.html",
        newsletter=newsletter,
        identity_display=None,
        identity_email=None,
        is_admin=False,
        is_super_admin=False,
    )


@app.post("/ingest")
async def http_ingest(request: Request, to: str):
    """Push-based / manual ingestion -- same shared-secret gate as before, now calling
    the D1-backed orchestration in worker_entry.py (the counterpart to ingest.ingest()).
    """
    if INGEST_TOKEN and request.headers.get("X-Ingest-Token") != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing ingest token")

    raw_bytes = await request.body()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")

    from worker_entry import ingest_via_d1

    newsletter = await ingest_via_d1(raw_bytes, to, _db(request))
    return {"slug": newsletter.slug, "subject": newsletter.subject}
