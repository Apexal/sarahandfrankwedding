# pages

Wedding site content and the public pages: home, welcome (password), schedule, registry, FAQ, photos, and the
`/<slug>/` prose-page catch-all. `PLAN.md` sections 6 and 7 are the spec.

- **`WeddingSettings` is the one cached singleton.** Read it via `WeddingSettings.load()`; `save()` forces pk=1
  and busts the cache. `core.account_adapter.SiteIdentity` reads `couple_names` from it for allauth's emails.
- **`site_password` blank means the gate is off.** It stays blank on production until the invitations mail.
  `site_password_fingerprint` is what the middleware will store in the session, so rotating the password
  re-locks everyone. Compare with `check_site_password()`, never by hand: it strips and casefolds.
- **`rsvp_is_open(event)` is the one place open/closed is decided.** An event's own `rsvp_deadline` overrides
  the wedding-wide one; no deadline anywhere means open while `rsvp_open` is on.
- **`Page.slug` cannot be a fixed route.** `RESERVED_PAGE_SLUGS` is enforced by a field validator because the
  catch-all is last in `urlpatterns`. Add to the set whenever a fixed route is added.
- CKEditor: `extends` on `Page.body` (staff-only), `default` everywhere else. Render with `.ck-content` and
  `|safe` per the root `CLAUDE.md`.
- `guests` imports this app. Never import `guests.models` at module level here.
- **The gate lives in `pages/middleware.py`** and decides in `process_view` from `request.resolver_match`: staff bypass;
  namespaces `admin`, `anymail`, `save_the_date`, `djdt`, `django_browser_reload`; url names starting `account_`/`mfa_`
  (allauth has no namespace), `pages:welcome`, `health`, `robots_txt`, `favicon_ico`; and `STATIC_URL`/`MEDIA_URL`
  prefixes. Exemptions are checked before `WeddingSettings.load()` so `/health/` answers with the database down.
- **Templates:** `pages/templates/pages/base_public.html` extends `core/base.html` and carries the nav and footer;
  every guest-facing template in any app extends it and fills only `head_title` and `content`. The nav reads
  `nav_pages` from `pages.context_processors.wedding`.
- **Theme tokens live in `static/css/input.css`** (`@plugin "./daisyui-theme.mjs" { name: "wedding"; ... }`), not in
  the `.mjs`, which `make setup` re-downloads. Rebuild with `make css` after template changes; `output.css` is committed.
- **Guest-facing pages use the site's own component classes, not daisyUI's.** `body.site` (set by `base_public.html`),
  `button button-primary|button-outline`, `field-input`, `choice`/`choice-input`, `text-link`, `notice`, `eyebrow`,
  `page-title`, `nav-link`, and the `motion-*` reveal classes are in `input.css`. daisyUI's components sit in a
  sublayer of `utilities`, so `.btn`/`.input` would out-rank anything in `@layer components`; keep them for the
  staff pages. Page tops go through `pages/includes/page_header.html`, rules through `pages/includes/ornament.html`.
- **Fonts are self-hosted** in `static/fonts/` (Pinyon Script, Cormorant Garamond, Jost; SIL OFL): the CSP's
  `font-src` and `script-src` are `'self'` only, so no Google Fonts link and no inline `<script>`.
- **The save-the-date envelope** is `static/css/save-the-date.css` (imported by `input.css`) plus
  `static/js/save-the-date.js`; the templates are `guests/save_the_date/page.html` (the stage) with
  `_envelope.html` and `_letter.html` (which includes `_form.html`). The stage renders `is-sealed` on a GET
  and `is-done` after any POST; the envelope rules only apply under `html.js`, so without JavaScript the
  letter is a plain page. To work on the letter without the animation, use `/save-the-date/<code>/letter/`
  (or `/save-the-date/letter/` while the generic page is open): same view, opened, no envelope.
