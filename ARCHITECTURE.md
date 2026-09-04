# Architecture

- `src/newsletter_archive/parser.py`, `sanitizer.py`, `slug.py` — pure logic, no I/O:
  MIME parsing (stdlib `email`), unsubscribe/preferences link neutralization and
  link/target rewriting (BeautifulSoup + `html.parser`), permalink slug generation.
  Shared by both paths below.
- `src/newsletter_archive/access.py` — resolves the caller's Cloudflare Access identity
  server-side, via an internal `/cdn-cgi/access/get-identity` subrequest that forwards
  the incoming `Cookie` header (not a client-supplied header, so it can't be forged).
  Fails closed to "no identity" if there's no valid Access session.
- `src/newsletter_archive/web/app.py` — the FastAPI app, served inside the Worker via
  its built-in ASGI adapter. Routes: `/` (sender-card homepage), `/archive` (the full
  filterable/sortable/paginated list), `/n/{slug}` (permalink page, with a print view
  and Open Graph tags), `/n/{slug}/images/{content_id}`, `/n/{slug}/reprocess` (admin
  action: re-runs the parse → sanitize → link-resolve pipeline against the stored
  `raw_eml`), `/help` (in-app version of [USAGE.md](USAGE.md), any logged-in user),
  `POST /ingest`. Authorization has two tiers: super admins (`SUPER_ADMIN_EMAILS` in
  `wrangler.jsonc`; also the only ones who can see `/quarantine`, `/deleted`,
  `/admin/newsletters` and `/admin/actions`) and per-sender admin grants
  (`/permissions`, super-admin only to create) that let a user
  delete/backdate/reprocess/publish newsletters from senders they administer, manage
  that sender's settings, and edit/revoke anyone's embeds for those senders. Every
  logged-in user, regardless of grants, can view all newsletters (public or private) and
  create their own embeds for any sender.
- `/permissions` lists every grant (read-only for everyone but the super admin, who can
  add/revoke there) and `/embeds` lets any authenticated user publish a token-scoped,
  unauthenticated "recent newsletters" query (`/embed/{token}`, `/embed/{token}/n/{slug}`)
  for embedding as an iframe on a department site; both are linked from the header for
  every logged-in user. `GET /admin` is kept only as a redirect to `/permissions` for
  old bookmarks. The `/embed/*` routes deliberately skip the Access identity check and
  sit behind an Access Bypass policy scoped to `/embed/*` in the dashboard -- the
  unguessable token, re-validated against the newsletter's sender on every request, is
  the actual security boundary, not Access.
- **Visibility** — every newsletter carries `visibility` (`public`/`private`). Private
  keeps it on the authenticated site but off every public surface; that's enforced by
  `list_newsletters(public_only=True)` on each public feed plus a `visibility` check on
  each public permalink route, so the authenticated site is deliberately unfiltered. The
  ingest default resolves in one place, `storage_d1.resolve_default_visibility`: an
  explicit `sender_settings.default_visibility` wins, else public if
  `sender_has_admin(from_email)` and private if not — URL secrecy can't substitute for
  this, since `slug.make_slug` derives its suffix from `sha256(message_id)` and the
  sender controls their own `Message-ID`.
- **Sharing a single newsletter** — `/embed/s/{key}` (that sender's public newsletters)
  and `/embed/s/{key}/n/{slug}` (one public newsletter), where `key` is an unguessable
  `sender_settings.share_key` an admin mints and can revoke. Deliberately not an
  `embed_queries` row: it needs no configuration and shouldn't clutter `/embeds`. Both
  sit under `/embed/*` so the existing Access Bypass policy covers them, and both
  re-validate that the newsletter is public and actually belongs to the key's sender.
  Two/four path segments, so neither collides with `/embed/{token}` or
  `/embed/{token}/n/{slug}`.
- `/quarantine` (super-admin only) — newsletters from senders outside `*.asu.edu` land
  here instead of the public site (homepage, archive, every embed) until whitelisted;
  see `worker_entry._is_asu_sender` and `storage_d1.sender_allowlist`. `/deleted`
  (super-admin only) — "deleting" a newsletter marks it (`deleted_at`/`deleted_by`)
  rather than removing it, and it can be restored from here.
- `/admin/newsletters` and `/admin/actions` (super-admin only) — every newsletter ever
  ingested ordered by `created_at` (including quarantined/deleted, with status), and the
  audit trail: one row per modification plus a login row per person per rolling 4-hour
  window, written by `storage_d1.log_action` / `log_login_if_new_session` (the latter
  hooked into `_current_user`, the single place every authenticated route resolves
  identity).
- `src/newsletter_archive/storage_d1.py` — async, D1-backed storage used by the deployed
  routes and the email handler. Tables: `newsletters` + `newsletter_images` (core
  archive), `newsletter_admins` (per-sender admin), `embed_queries` (public embeds),
  `resolved_links` + `mirrored_assets` (link/image durability caches), `sender_allowlist`
  (quarantine bypass), `sender_settings` (per-sender visibility default + share key),
  `action_log` (audit trail).
- `src/worker_entry.py` — the Worker entrypoint: `fetch` hands off to the FastAPI app;
  `email` (Cloudflare Email Routing's inbound trigger) reads the raw MIME and calls
  `ingest_via_d1()`, which runs the same parse → resolve-links → sanitize → slug → store
  pipeline (this is also where the `*.asu.edu`/allowlist quarantine check and the
  visibility default are resolved, so both the email trigger and `POST /ingest` are
  covered by one check). Tracked/redirect
  links (e.g. Mailchimp click-tracking) are followed to their real destination before
  sanitizing, since a tracked href reveals nothing about where it actually goes.
  Workers cap total subrequests per invocation, so resolution is concurrency-throttled
  and capped per run; results are cached in the `resolved_links` table so a repeat run
  (or a manual Reprocess) picks up newly-resolved links incrementally instead of
  re-spending budget on ones already solved. Externally-hosted images are similarly
  mirrored into R2 and cached in `mirrored_assets`.
- `src/newsletter_archive/storage.py` + `ingest.py` + the CLI (`python -m
  newsletter_archive.ingest`) — a separate, sync/sqlite3 path used only by the pytest
  suite as a fast, wrangler-free way to test the shared parsing/sanitizing logic. Not
  part of the deployed Worker.

Everything under `src/` is what the Worker bundler walks and deploys; `tests/`,
`migrations/`, and local venvs deliberately live outside it -- the Python bundler walks
the entry file's directory for modules to vendor with no tree-shaking, so anything
outside `src/` (local venvs, a project-local `node_modules/`, etc.) would otherwise get
swept into the deploy bundle.
