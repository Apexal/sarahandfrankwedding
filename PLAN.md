# Technical Plan: Sarah & Frank wedding site

Status: **v8, 2026-09-03: code implemented** (v1 2026-09-01; v2 to v6 earlier today). Domain: **sarahandfrank.wedding**.
Wedding: **Saturday 2027-09-18** (`WEDDING_DATE` in `core/constants.py`).

This repo is the Django 6 starter described in `CLAUDE.md`, deployed to Fly.io. This plan turns it into the
public-facing wedding site plus the guest-management platform behind it. Everything is scoped backward from
two dates that cannot be undone: the digital save-the-date send on **2026-09-30** (a sent message cannot be
recalled) and the invitation print in the **first week of January 2027** (a printed card cannot be
reprinted). The first is a target that can slip a week at the cost of a week of address collection; the
second is hard.

---

## 0. Change log

**v8 (2026-09-03, night).** PostHog and maintenance mode removed. Domain events and handled exceptions go to
structlog (the Fly logs) instead. With maintenance mode gone, `core.AppSettings` held only a site name and
support email that fed allauth's emails, so those moved to `WeddingSettings` (`couple_names`, `contact_email`) and
`AppSettings` was deleted: the project has one singleton.

**v7 (2026-09-03, night).** The code in sections 4 to 12 is implemented (see "Implementation status" under
section 14): data model and admin, public site and gate, save-the-date pages, campaigns and bounces, RSVP flow and
matcher, dashboard, exports, QR codes, seating. Starter features that only make sense for a client project were
removed as well: django-auditlog, impersonation, staff roles and `sync_groups`, the registration switch, per-user
timezones and the profile page, the mobile-API leftovers (tokens, verification codes, deletion requests), faker
seeding, and `setup_project.sh`. Kept: staff MFA, structlog, import-export, CKEditor, release-please (PostHog and
maintenance mode went in v8). `steady_queue` evaluates cron in UTC, so the campaign resume runs at 13:00 UTC.

**v6 (2026-09-03, later).** Closed the gap where a guest who lost the save-the-date text and typed the URL
on another device would hit a password page with no password. The gate now stays off until the invitations
mail (nothing on the site is private before the travel and hotel pages, which are published that same day),
the password page then points to the invitation and `contact_email`, and every guest-facing email carries the
household's personal link in its footer.

**v5 (2026-09-03, later).** Minimal seating added as a Phase 5 item: a `Table` model and `Guest.table`,
assignment through the guest changelist, a `/manage/seating/` page with capacity and household-split
warnings, and a seating export for the caterer and escort cards. No drag and drop. About 3 hours in July
2027; nothing sent or printed depends on it.

**v4 (2026-09-03, evening).** Timeline compressed to match the real plan: digital save-the-date by
2026-09-30, one year out; physical invitations mailed late January to mid February 2027.

- Freeze 1 (save-the-date send) moves from December to **2026-09-30**. Freeze 2 (invitation print) moves
  from June to **2027-01-05**. Address collection runs October to early January.
- Phases reordered: guest data and the save-the-date page ship before the public site. Theme, gate, pages,
  and starter cleanup follow in October. RSVP must be live when invitations land, so Phase 3 is November to
  December with a 2026-12-20 acceptance date.
- The save-the-date email carries only the personalized link, which unlocks the site by itself, so the
  **site password is now frozen at Freeze 2**, not Freeze 1. It can be chosen any time before the invitation
  proof.
- The sending domain will be about three weeks old on send day. Texts from our phones become the primary
  channel for the save-the-date; the email campaign runs at 25 per day alongside. System email is the primary
  channel only from the November address nudges on, when the domain has aged two months.
- September carries about 18 hours of work. The plan names the minimum that must ship for the send and what
  slips to October if it runs long.

**v3.** Save-the-date is digital and doubles as the address collector via `/save-the-date/<code>/`;
campaigns moved earlier; `address_confirmed_at`, `missing_address` audience, `address_nudge` template, `.ics`,
links export for texting.

**v2.** Photos: shared Google Photos album, no build. Events: ceremony, reception, subset-only rehearsal
dinner. Content: Sarah edits prose in the admin. Gate: one shared site password. Corrections after reading
the starter: bounce handling uses anymail's tracking webhook; `anymail` is only installed when `DEBUG=False`;
`robots.txt` disallows only `/admin/` and `/accounts/`; CI already runs `makemigrations --check`; staging has
`min_machines_running = 0`; removing `core/api/` also touches docstrings in `core/audit.py` and
`core/account_adapter.py`.

---

## 1. Goals and non-goals

**Goals**

- A digital save-the-date we send by text and email on 2026-09-30: names, date, city, "formal invitation to
  follow in the new year", add to calendar, and a form that collects the household's mailing address, email,
  and phone for the physical invitation. Personalized per household so we can see who has not answered and
  nudge them.
- A public site guests reach from the save-the-date and invitation: story, schedule, travel and stay,
  registry links, FAQ, photos link. Password-gated so hotel block codes and venue details stay private.
- RSVP without accounts, live the day invitations land: type your name, we find your household, answer per
  guest and per event, answer our questions (meal, dietary, song request, hotel), get a confirmation email.
  Editable until the deadline, because plans change over a seven-month window. Works for an older relative
  on an old phone.
- The guest list as a system of record we own: households, guests, addresses, invitation scope, RSVP state,
  answers. Import from the spreadsheet we already have; export CSV for the printer, the caterer, and the
  seating chart.
- Reminders (for addresses, then for RSVPs) sent in paced batches that will not land us in spam.
- Seating: assign attending guests to reception tables next to their meal and dietary answers, with
  warnings for over-capacity tables, split households, and seated guests who later declined; export for the
  caterer and the escort cards.
- A single link and QR code to a shared photo album for the reception tables.
- A management dashboard for the two of us only, behind the staff MFA the starter already enforces.

**Non-goals (cut deliberately)**

- Guest accounts of any kind. `AppSettings.allow_registration` stays off forever; allauth exists only so the
  two of us can log in.
- Sending SMS from the system. Texts go from our own phones using links the system prepares (section 10).
- Hosting guest photos. Registry (external links only). Drag-and-drop seating (assignment is a dropdown per
  guest). Gift tracking. Multi-language. Native app. Anything we would not be embarrassed to do by hand for a
  one-time event.

**Honest framing.** Zola, Joy, and The Knot give the site, RSVP, digital save-the-dates, and guest list away
free and monetize on registries and paper. Building this buys ownership of the guest data, no upsells, and
flows that do not push Aunt Carol to create an account. It costs roughly 49 hours (section 14), 18 of them in
September. The compensating rule: every phase ends with something deployed and usable, so we can stop at any
point and still have a working site.

---

## 2. Hard dates and the two freezes

Working backward from 2027-09-18:

| Date | Milestone | What must be deployed and tested |
|---|---|---|
| 2026-09-06 | Phase 0a done | Real domain over HTTPS; Resend domain verified; SPF, DKIM, DMARC live. Every day of aging before the send counts |
| 2026-09-20 | Phase 1 done | The real spreadsheet imported on production: every household by name, side, events, and whatever emails and phones we have. Households with no contact at all handed to parents to fill in |
| 2026-09-27 | Phase 2a done | Save-the-date pages, `.ics`, links export, campaign system; the save-the-date email rehearsed on staging against the recipient allowlist; three relatives open a personalized link on their phones |
| **2026-09-30** | **Freeze 1: digital save-the-date goes out** | Texts from our phones to every household with a phone; email campaign at 25 per day to households with an email; generic link in family group chats. From this day the domain, `/save-the-date/` path, and household codes cannot change |
| 2026-10-31 | Phase 2b done | Site behind the gate; theme; pages; starter cleanup; bounce receiver; dashboard address section; Sarah's superuser |
| 2026-11-01, 11-22, 12-10 | Address nudges | `address_nudge` campaigns to `missing_address`; week of 12-14, phone calls to whoever is left |
| 2026-12-20 | RSVP flow accepted | Ten relatives RSVP unassisted on staging; `rsvp_match_check` reports zero misses on the real list |
| **2027-01-05** | **Freeze 2: invitation print** | Every household has `address_confirmed_at`; site password, `/rsvp/`, invite codes, and RSVP deadline final; addresses and codes exported for the printer |
| ~2027-01-25 to 02-15 | Invitations mail | The day the first batch mails: `site_password` set and the gate turns on, `rsvp_open` flipped, travel and hotel pages published. Until then the site is open and holds nothing private |
| 2027-03 | Photo album created and linked | Tested by relatives by 2027-06-01 |
| ~2027-05-15 | Mid-window `announcement` email | Schedule, hotel block, "RSVP if you haven't" |
| 2027-08-01, 08-10 | RSVP nudges | To `non_responders` and `missing_answers` |
| 2027-08-14 | RSVP deadline printed on the card | |
| 2027-08-15 to 09-01 | Seating | Every attending guest assigned on `/manage/seating/`; warnings cleared |
| ~2027-09-03 | Final counts and seating chart to caterer and venue | `/manage/export/rsvps.csv` and `/manage/export/seating.csv` |
| 2027-09-18 | Wedding | Table cards carry the album QR |
| 2027-10-31 | Wind-down | Album downloaded, guest data exported, machine scaled down or kept as a keepsake |

Rule: nothing sent or printed depends on code that is not deployed to production and tested by the freeze
that covers it. "Deployed to production" means the release-please PR is merged and the Fly deploy is green
(section 13), not merged to `main`, which only deploys staging.

---

## 3. What is sent or printed, and when it freezes

This table is why the freezes exist. Anything in it is a constant from its send or print date on.

| Item | Date | Channel | Carries | Frozen by |
|---|---|---|---|---|
| Digital save-the-date | 2026-09-30 | Texts from our phones (primary); email campaign at 25 per day; family group chats | Personalized link `https://sarahandfrank.wedding/save-the-date/<code>/` (or the generic `/save-the-date/`), names, date, city, "formal invitation to follow in the new year", "please confirm your mailing address", add to calendar, a "visit our website" button that unlocks the site | Freeze 1 |
| Invitation + RSVP card | print ~2027-01-05, mail late Jan to mid Feb 2027 | Post | Domain, site password, RSVP deadline (2027-08-14), "RSVP at sarahandfrank.wedding/rsvp/ using your name or the code below", per-household `invite_code`, optionally a QR to `/welcome/?k=<password>` | Freeze 2 |
| Table cards | Sep 2027 | Print | QR to the Google Photos album link (not to our site) | none |

The site password is deliberately a single low-security shared secret: it keeps hotel block codes off search
engines and away from strangers, nothing more. The save-the-date link unlocks the site without it (section
7), so the password is typed only by people arriving from the printed invitation, and it can be chosen any
time before the invitation proof. The gate itself stays off until the invitations mail: before the travel
and hotel pages exist there is nothing to protect, and a wall would shut out any guest who lost their
save-the-date text. It is checked case-insensitively so "Willow2027" and "willow2027" both work. Choose
something short, pronounceable, and free of 0/O and 1/l ambiguity.

Household codes do double duty: they are the token in the save-the-date link and the code printed on the
invitation. Regenerating one after Freeze 1 would break a link somebody saved, so the "Regenerate invite
code" admin action is disabled by `WeddingSettings.codes_frozen` from the send date.

---

## 4. What the starter gives us: keep / change / remove

**Keep as-is.** `core.User` + hardened admin, staff MFA (`RequireStaffMfaMiddleware`), allauth email login
for the two of us, structlog, the `messaging`
pipeline (`OutboundEmail` audit trail + the rate-limited single-threaded `email` queue on `steady_queue`),
Tigris storage + the `core/checks.py` endpoint guard, `django-import-export`, CKEditor 5, Fly deploy with a
staging app, CI (ruff, `makemigrations --check`, pytest), release-please.

**Change before the send (Phases 1 and 2a).**

- `CSRF_TRUSTED_ORIGINS` is `[f"https://{APP_NAME}.fly.dev"]` today (`settings.py`), so a POST from the
  custom domain fails CSRF, and the save-the-date form is a POST. Derive it from `APP_BASE_URL` and keep the
  `.fly.dev` origin as a second entry. `ALLOWED_HOSTS` is already env-driven; add the domain via the Fly
  secret.
- **Staging email safety**, before real guest emails exist on staging. Staging runs `DEBUG=False`, so
  anymail/Resend sends real mail. Add an `EMAIL_RECIPIENT_ALLOWLIST` setting (comma-separated env var)
  honoured in `messaging/tasks.py::_send_outbound_email`: when set and the recipient is not on the list,
  deliver to the first allowed address and prefix the subject with `[to: original@example.com]`. The
  `OutboundEmail` row keeps the original recipient so the audit trail stays truthful. Set the variable on
  staging only. Add a `core/checks.py` error that fires when `FLY_APP_NAME` contains `staging` and the
  allowlist is empty, so the deploy fails rather than emailing a real guest.
- `WeddingSettings` singleton (section 6), with the fields the save-the-date page needs.
- A minimal theme: the `wedding` DaisyUI theme in `static/css/daisyui-theme.mjs` as default in
  `static/css/input.css`, two self-hosted woff2 typefaces under `static/fonts/` (CSP `font-src` is `'self'`),
  enough that the save-the-date page and the placeholder home look like a wedding, not a starter.
- Replace `core/templates/core/index.html` with a placeholder home (names, date, "more soon"). Remove the
  `home` route from `core/urls.py` when `pages` takes `/` in Phase 2b.
- `core/views.py::robots_txt` currently disallows `/admin/` and `/accounts/`. Change to `Disallow: /` and add
  `<meta name="robots" content="noindex, nofollow">` to `core/base.html`. Five minutes; do it now.
- `pyproject.toml` ruff `known-first-party` gains `pages` and `guests`.

**Change after the send (Phase 2b).**

- `core/constants.py`: `COMPANY["name"]` already reads "Sarah and Frank" and feeds `company.name` in the
  navbar and `<title>`. Leave it; the editable copy lives in `WeddingSettings.couple_names`.
- `anymail` is appended to `INSTALLED_APPS` only in the `not DEBUG` branch. Install it unconditionally (the
  backend selection stays conditional) so the webhook URLs in section 11 resolve in every environment.
- Navbar: replace the login / profile / sign-up UI in `core/templates/core/includes/navbar.html` with the
  wedding nav. `/accounts/login/` stays reachable by URL for us; the impersonation and environment banners in
  `core/base.html` stay.
- Create Sarah's superuser on production (`fly ssh console -C "python manage.py createsuperuser"`). The
  starter emails a verification link on first login. Both of us enroll TOTP and store recovery codes in the
  shared password manager; two superusers means either can reset the other.

**Removed (done 2026-09-03).** Nothing here serves a wedding for two administrators and a few hundred guests:

- `agreements` app, the mobile API (`core/api/`, `bruno-http/`, `django-ninja`) and its leftovers in `core`
  (`AccessToken`, `RefreshToken`, `EmailVerificationCode`, `AccountDeletionRequest`, `core/deletion.py`, the
  deletion sweep task), and the empty `received-webhooks/` directory.
- django-auditlog everywhere (history fields, registrations, admin history views, the audit middleware, the
  nightly prune, `core/audit.py`). The messaging `OutboundEmail` trail and structlog remain the record of what
  happened.
- Impersonation (admin view, session key, middleware, banner), staff roles and `sync_groups`, the
  `AppSettings.allow_registration` switch (signup is closed unconditionally in the account adapter), per-user
  timezones and `UserTimezoneMiddleware`, the profile page, faker-based seeding, and `setup_project.sh`.
- PostHog (events and exception capture now go to structlog), maintenance mode, and with it the whole
  `core.AppSettings` singleton: allauth's site identity now reads `WeddingSettings.couple_names` and
  `contact_email`.

---

## 5. Architecture overview

Two new apps as siblings of `core` and `messaging`:

| App | Owns |
|---|---|
| `pages` | Public site: home, schedule, registry, FAQ, photos link, password page, prose pages. Models `WeddingSettings`, `Page`, `FaqItem`. `SitePasswordMiddleware`. |
| `guests` | System of record: `Household`, `Guest`, `Event`, `Rsvp`, `RsvpQuestion`, `RsvpAnswer`, `Table`, `ContactSubmission`, `Campaign`, `CampaignRecipient`. Save-the-date pages (`/save-the-date/`), RSVP flow (`/rsvp/`), staff dashboard and seating (`/manage/`), import/export, campaigns, bounce receiver, backups. |

`WeddingSettings` lives in `pages` but is needed by the save-the-date in Phase 2a, before the rest of
`pages` exists. Create the `pages` app in Phase 1 with just the singleton and its admin; the views and other
models arrive in Phase 2b.

**Guest-facing URL inventory.** Paths marked *frozen* are in a sent message or on paper.

| Path | Purpose | Gate | Frozen |
|---|---|---|---|
| `/save-the-date/<code>/` | Personalized save-the-date + address form | code in the URL; exempt from the site password | Freeze 1 |
| `/save-the-date/` | Generic save-the-date + address form, for forwarding | none while `save_the_date_open`; exempt from the site password | Freeze 1 |
| `/save-the-date/event.ics` | Add to calendar | none | no |
| `/` | Home | site password, from the day invitations mail | Freeze 1 (domain) |
| `/welcome/` | Password page; `?k=` unlocks from a link or QR | none | Freeze 2 |
| `/schedule/`, `/registry/`, `/faq/`, `/photos/` | Structured pages from templates | site password | no |
| `/<slug>/` | `Page` rows (story, travel, wedding-party, things-to-do) | site password | no |
| `/rsvp/`, `/rsvp/lookup/`, `/rsvp/select/<id>/`, `/rsvp/respond/`, `/rsvp/done/`, `/rsvp/reset/` | RSVP flow | site password, then household lookup | `/rsvp/` at Freeze 2 |
| `/manage/`, `/manage/seating/`, `/manage/seating/print/`, `/manage/export/rsvps.csv`, `/manage/export/seating.csv`, `/manage/export/save-the-date-links.csv`, `/manage/qr/<name>.svg` | Dashboard and seating | staff session + MFA | no |
| `/admin/`, `/accounts/` | Django admin, allauth | staff session + MFA | no |
| `/anymail/resend/tracking/` | Resend webhook | anymail basic-auth + signature | no |
| `/health/`, `/robots.txt`, `/favicon.ico` | Infra | none | no |

**Session keys.** All guest state lives in Django's DB-backed session; nothing identifies a guest except
these.

| Key | Set by | Meaning |
|---|---|---|
| `site_unlocked` | `/welcome/`, or any save-the-date page | Fingerprint of the current site password; mismatch re-locks |
| `rsvp_candidates` | `/rsvp/lookup/` | Household ids offered by the last lookup; `select` accepts only these |
| `rsvp_household_id` | `/rsvp/select/`, or a personalized save-the-date page | The household being answered for |

**Runtime.** Fly proxy → gunicorn (3 workers) → Django. Background work on the `steady_queue` sidecar
(`default` queue, 3 threads; `email` queue, 1 thread, paced by `EMAIL_RATE_LIMIT_PER_SECOND`). Storage: one
private Tigris bucket per environment, used only for backups and import/export temp files. DB: Postgres on
Fly; SQLite locally and in tests.

**Repository layout after the build.**

```
pages/      models.py (WeddingSettings, Page, FaqItem), middleware.py (SitePasswordMiddleware), views.py,
            urls.py, admin.py, context_processors.py (wedding_settings, nav pages), templates/pages/
guests/     models.py, matching.py, nicknames.py, forms.py (address form, RsvpForm builder), admin.py,
            resources.py, views/save_the_date.py, views/rsvp.py, views/manage.py, views/seating.py, ics.py,
            campaigns.py,
            signals.py (anymail tracking), tasks.py, email_contexts.py,
            management/commands/rsvp_match_check.py, migrations/000X_seed_email_templates.py,
            templates/guests/, templates/guests/email/
core/       ratelimit.py (new, cache-backed per-IP limiter used by every public POST)
static/     css/ (theme), fonts/, img/ (hero, save-the-date, story photos; committed and optimized)
```

---

## 6. Data model

The rule from the vendor research: **the unit of invitation is the household; the unit of RSVP is
(guest, event).** Getting this wrong is the thing we would regret most, so it is spelled out.

### `guests`

```python
class Household(TimestampedModel):
    name                 # as it appears on the envelope: "The Smiths", "Bob Jones & Guest"; unique
    invite_code          # 6 chars, unique, alphabet without 0/O/1/I/L, generated in save(). Token in the
                         # save-the-date link and the code on the invitation; frozen at Freeze 1.
    side                 # sarah | frank | both  (list filter + dashboard split)
    email, phone         # primary contact for the save-the-date, confirmations, and nudges; optional
    email_opt_out        # bool; set by hand if someone asks us to stop emailing
    email_bounced_at     # set by the anymail tracking receiver; campaigns skip it; dashboard lists it
    address_line1, address_line2, city, state, postal_code, country
    address_confirmed_at # set when the household submits the save-the-date form, or by us after a call;
                         # null = still need an address for the invitation
    invited_events       # M2M Event: what this household is invited to
    responded_at         # set on first RSVP submission; null = non-responder
    notes                # private
    history = AuditlogHistoryField()

class Guest(TimestampedModel):
    household            # FK, CASCADE
    first_name, last_name
    is_plus_one          # name may be blank until they RSVP ("Guest of Bob Jones")
    is_child
    also_known_as        # "Bob, Bobby" — comma-separated, consulted by the matcher
    email                # optional; household.email is what we send to
    table                # FK Table, nullable, SET_NULL: the reception seat assignment (Phase 5)
    sort_order
    first_name_normalized, last_name_normalized   # casefolded, accent-stripped; set in save(); indexed

class Event(TimestampedModel):
    name, slug, starts_at, ends_at, venue_name, address
    description          # CKEditor5Field("extends"); staff-only authoring, so sourceEditing is fine
    dress_code
    is_private           # rehearsal dinner: shown on the schedule only to households invited to it
    rsvp_required
    rsvp_deadline        # nullable; falls back to WeddingSettings.rsvp_deadline
    order

class Table(TimestampedModel):   # Phase 5; reception only
    name                 # "Table 1", "Head table"
    capacity
    order, notes

class Rsvp(TimestampedModel):
    guest, event         # unique together
    attending            # bool. No row = not answered yet.
    responded_at
    source               # guest | admin  (phone RSVPs entered by us)

class RsvpQuestion(TimestampedModel):
    label, help_text
    kind                 # short_text | long_text | single_choice | multi_choice | yes_no
    choices              # JSON list of strings, for the choice kinds
    scope                # per_guest | per_household
    only_if_attending    # FK Event, nullable: ask only when this guest is attending this event
    show_on_dashboard    # roll up choice counts / list text answers on /manage/
    required, order, active

class RsvpAnswer(TimestampedModel):
    question, household
    guest                # nullable when scope is per_household
    value                # JSON: string, list of strings, or bool
    # unique (question, household, guest)

class ContactSubmission(TimestampedModel):   # only from the generic save-the-date page
    names                # free text as typed: "Bob & Carol Jones"
    email, phone, address fields, notes
    status               # new | merged | ignored
    household            # FK, SET_NULL, set when merged
    ip

class Campaign(TimestampedModel):
    name, template_key           # messaging template key (section 11)
    audience                     # all_with_email | missing_address | non_responders | missing_answers |
                                 # attending | declined | selected
    event                        # FK, nullable; required for attending
    households                   # M2M, used only by selected
    daily_cap                    # default 25 until the domain has aged; 50 from November
    status                       # draft | sending | sent
    sent_at, created_by

class CampaignRecipient(TimestampedModel):
    campaign, household          # unique together
    outbound_email               # FK messaging.OutboundEmail, SET_NULL
    status                       # queued | sent | failed | skipped
    error
```

The canonical question set, to show the model fits:

| Question | kind | scope | only_if_attending | required |
|---|---|---|---|---|
| Meal choice (beef / fish / vegetarian / kids) | single_choice | per_guest | reception | yes |
| Dietary restrictions or allergies | short_text | per_guest | reception | no |
| A song that will get you dancing | short_text | per_household | reception | no |
| Are you staying at the hotel block? | yes_no | per_household | — | no |
| Anything else we should know? | long_text | per_household | — | no |

Notes:

- **Per-guest invitation exceptions** (one member of a couple invited to the rehearsal dinner, the other
  not) are handled by splitting them into two households. A per-guest override that every query has to
  honour is not worth it for a handful of cases. Say so in the admin help text for `invited_events`.
- **Pending is the absence of a row.** Dashboard counts per event are invited guests (household M2M ×
  guests) minus `Rsvp` rows. If we add an event to a household after they responded, that event correctly
  shows as pending for them even though `responded_at` is set; the `missing_answers` audience exists for
  exactly this case.
- **Address state is one timestamp.** An address typed in from the spreadsheet has `address_confirmed_at`
  null until the household confirms it on the save-the-date page or we tick it after a phone call. The
  `missing_address` audience and the dashboard both key on that field, not on whether the fields are blank.
- **Child guests** carry `is_child` only; kids' meals are a choice on the meal question.
- **Seating is reception-only.** `Guest.table` is one assignment per guest. Seating a second event would
  mean a (guest, event, table) row instead; not worth it for a rehearsal dinner.
- **Auditlog:** register `Household`, `Guest`, `Event`, `Rsvp`, `RsvpQuestion`, `RsvpAnswer`, `Campaign`
  with `auditlog.register(...)` at the bottom of `guests/models.py`, the way `core/models.py` does. A
  household saving its own address from the save-the-date page is recorded with no actor; admin edits
  attribute to us.
- **Indexes:** `Guest(last_name_normalized)`, `Household(invite_code)` (unique), `Rsvp(event, attending)`.

### `pages`

```python
class WeddingSettings(TimestampedModel):   # singleton like AppSettings: pk=1 forced in save(), cached, load()
    couple_names              # "Sarah & Frank"; default from COMPANY["name"]
    wedding_date              # default WEDDING_DATE
    wedding_city              # "Chicago, Illinois" — the only location detail the save-the-date reveals
    site_password             # plain text: a shared low-security secret that we need to read back in the
                              # admin. Blank = gate off; stays blank until the invitations mail. Frozen at
                              # Freeze 2.
    codes_frozen              # bool; disables "Regenerate invite code" once links are in the wild
    save_the_date_open        # bool; the generic /save-the-date/ page 404s when off
    save_the_date_message     # CKEditor5Field("default"): the paragraph on the save-the-date page
    rsvp_open, rsvp_deadline  # deadline is a date; the form closes at the end of that day in TIME_ZONE
    rsvp_intro                # CKEditor5Field("default"), shown above the name form
    rsvp_closed_message       # CKEditor5Field("default"), shown when closed or past the deadline
    photo_album_url           # the Google Photos shared-album link; blank hides the Photos nav item
    photo_album_note          # short text under the button ("You'll need a Google account to add photos")
    registry_links            # JSON [{label, url}]
    contact_email             # shown in the footer; Reply-To on every guest email; receives internal notices

class Page(TimestampedModel):
    slug, title, body (CKEditor5Field "extends"), nav_label, nav_order, show_in_nav, published
    # clean(): slug must not collide with a fixed route (rsvp, save-the-date, schedule, welcome, faq,
    # registry, photos, manage, admin, accounts, health, anymail, ckeditor5, static, media)

class FaqItem(TimestampedModel):
    question, answer (CKEditor5Field "default"), order, published
```

Pages with structure (home, schedule, registry, FAQ, photos, RSVP, save-the-date) are templates with fixed
routes. Prose-only pages (our story, travel and stay, wedding party, things to do) are `Page` rows served at
`/<slug>/` by a catch-all placed last in `pages/urls.py`, which is itself the last include in the project
`urlpatterns`. Sarah edits copy in the admin without a deploy.

`WeddingSettings` is the project's only singleton (the starter's `AppSettings` was removed); a `pages` context
processor exposes it to every template as `wedding`, and allauth's emails take their site name and support address
from `couple_names` and `contact_email`.

---

## 7. Public site and password gate (`pages`, Phase 2b)

**`SitePasswordMiddleware`**, placed immediately after `AuthenticationMiddleware`.

- Behaviour: when `WeddingSettings.load().site_password` is non-empty and
  `request.session.get("site_unlocked")` does not equal the current password's fingerprint
  (`sha256(password.casefold()).hexdigest()[:16]`), redirect to `/welcome/?next=<path>`. HTMX requests get
  `HX-Redirect` via `HttpResponseClientRedirect`, the way `AgreementGateMiddleware` does. Storing a
  fingerprint rather than a flag means rotating the password re-locks everyone; a save-the-date link
  re-unlocks, so rotating before Freeze 2 is harmless.
- Exempt: staff sessions; the `welcome` view; every `guests:save_the_date*` view and the `.ics`; the
  `admin`, `account`, `mfa`, and `anymail` URL namespaces; `health`, `robots_txt`, `favicon_ico`; anything
  served by whitenoise (its middleware runs before ours) or `MEDIA_URL`. Identify exempt routes by URL name
  and namespace in `process_view` (so `request.resolver_match` is populated), not by string prefixes.
- Unlocking: `POST /welcome/` checks `secrets.compare_digest` on stripped, casefolded input against the
  stripped, casefolded stored value; `GET /welcome/?k=<password>` performs the same check so a link or QR can
  unlock directly; and any save-the-date page view sets `site_unlocked` outright, because holding that link
  is the credential. 10 attempts per 15 minutes per IP on the form via `core/ratelimit.py`. logs `site_unlocked` (with `via`: form, link, save_the_date) and `site_password_failed`.
- **The gate is off until the invitations mail.** `site_password` stays blank on production from launch
  until the day the first invitation batch goes out (~2027-01-25). Before that the site holds names, date,
  story, FAQ, registry, and a schedule without addresses: nothing worth a wall, and a wall would shut out any
  guest who lost their save-the-date text and typed the URL on another device. The middleware ships in
  Phase 2b and is exercised on staging with a provisional password; production turns it on the same day the
  travel and hotel pages are published and every household has the password on paper.
- **Arriving without the password** after that day, a guest sees on `/welcome/`: "The password is on your
  invitation. Can't find it? Email us at <contact_email>." Every guest-facing email also carries the
  household's personal link in its footer (section 11), so an inbox search for "Sarah and Frank" is always
  a way in.

**Pages.**

- Home: names, date, countdown, hero image, RSVP call-to-action when `rsvp_open`, "details to follow" until
  the schedule is published.
- Schedule: `Event` rows in `order`; events with `is_private=True` appear only when the visitor's
  `rsvp_household_id` session value belongs to a household invited to them. A personalized save-the-date
  link sets that key, so those households see the rehearsal dinner from the start.
- Registry: `registry_links` as large buttons. FAQ: published `FaqItem` rows as a list, not an accordion
  (nothing hidden behind a tap). Photos: one big button to `photo_album_url` plus `photo_album_note`.
- Prose pages: `Page` rows rendered inside `.ck-content` with `|safe` per `CLAUDE.md`.
- Templates: restyle `core/templates/core/base.html` (the allauth layout extends it, so keep its blocks);
  `pages/base_public.html` extends it with the wedding nav and footer. Nav order: Home, Our Story, Schedule,
  Travel & Stay, Registry, FAQ, Photos, RSVP (a button, shown when `rsvp_open`).

**Design constraints.** Mobile-first; 18px base type; 44px tap targets; high contrast; no hover-only
affordances; every form is a real `<form>` post with HTMX as enhancement only. Exit criterion: Lighthouse
accessibility ≥ 95 on mobile for the save-the-date page (Phase 2a) and for home, schedule, and the RSVP form
(later phases).

**Assets.** Hero, save-the-date, and story images are committed, optimized static files (WebP with JPEG
fallback, ≤ 300 KB each) served through Fly's `[[statics]]` and whitenoise. The save-the-date email
references its image by absolute static URL, which the gate never blocks. Nothing guest-facing reads from the
bucket.

**Tests.** Gate is a no-op while `site_password` is blank; with one set it redirects an anonymous visitor
and passes staff; wrong password is rejected and rate
limited; correct password, `?k=`, and a save-the-date view each unlock; rotating the password re-locks an
unlocked session; save-the-date and health stay reachable while locked; a private event is hidden from a
visitor whose session household is not invited and shown to one that is; a `Page` slug colliding with a fixed
route fails validation.

---

## 8. Guest list management (`guests`: admin, import, export, dashboard)

**Admin (Phase 1, polished in 2b).**

- `HouseholdAdmin`: tabular `GuestInline`; `filter_horizontal` for `invited_events`; list columns name,
  side, guest count, invited events, address confirmed, responded, has email, has phone, bounced, and a
  copyable save-the-date link; list filters side, address confirmed, responded, invited event, has email,
  has phone, bounced; search across household name, guest names, and invite code. Actions: "Send campaign
  to selected" (creates a `selected` campaign in `draft` and opens it), "Mark address confirmed" (after a
  phone call), "Regenerate invite code" (disabled once `codes_frozen`), "Export invite codes CSV"
  (`household, invite_code, address…` for the printer). Until the dashboard exists, the "address confirmed"
  filter plus the link column is how we track October's collection.
- `GuestAdmin`: flat list with one annotated column per event showing yes / no / pending; from Phase 5,
  `table` in `list_editable` behind an "attending reception" filter, which is how seats get assigned.
- `RsvpAdmin`: read-mostly, for phone RSVPs entered with `source=admin`.
- `RsvpQuestionAdmin` for the question set; `EventAdmin`; `ContactSubmissionAdmin` (section 10);
  `CampaignAdmin` (section 11).
- All use `AuditlogHistoryAdminMixin` so the history view is available.

**Import (Phase 1).** A `django-import-export` `GuestResource`, one row per guest, household columns
repeated:

```
household, side, household_email, household_phone, address_line1, address_line2, city, state, postal_code,
country, events, first_name, last_name, is_plus_one, is_child, also_known_as, guest_email, notes
```

`events` is semicolon-separated `Event` slugs, which must already exist. `before_import_row` get-or-creates
the household by exact `name`; the first row for a household wins its address and contact fields. Guests are
matched by (household, first_name, last_name), so re-importing a corrected spreadsheet updates rather than
duplicates. Imported addresses do not set `address_confirmed_at`. The admin's dry-run preview is built in.
Test against the real spreadsheet before trusting it; the columns above are the target shape we reformat the
current sheet into. The list does not have to be final on 2026-09-20: households added later get their own
link, and the generic link covers anyone we forgot.

**Exports.**

- Household and Guest export via import-export (xlsx / csv) for address labels.
- `/manage/export/save-the-date-links.csv` (Phase 2a): `household, contact_names, phone, email, link,
  message`, where `message` is the prewritten text ("Hi Bob & Carol! Sarah and Frank are getting married on
  September 18, 2027 in <city>. Save the date, and please confirm your mailing address here: <link>"). This
  is what we paste into texts from our phones; on a Mac, Messages plus the Shortcuts app can walk the list.
- `/manage/export/seating.csv` (Phase 5): `table, guest, household, is_child, meal, dietary`, ordered by
  table then household, for the caterer and the escort cards.
- `/manage/export/rsvps.csv` (Phase 3) pivots per guest: `household, side, guest, is_child,
  attending:<event slug>` for each `rsvp_required` event, `q:<question label>` for each active question,
  `household_email, responded_at`. UTF-8 with BOM so Excel opens it cleanly. This is what the caterer and the
  seating spreadsheet consume; import-export cannot pivot a dynamic question set, hence the custom view.

**Dashboard `/manage/`** (`staff_member_required`; MFA enforced by the existing middleware; address section
in Phase 2b, RSVP sections in Phase 3):

- Address collection: confirmed / total households as a progress bar, split by side; the list of
  households still missing, each with its link, phone, email, and whether it has been emailed (via
  `CampaignRecipient`) so we know who to text or call.
- Per event: invited, yes, no, pending, as stat tiles with a progress bar.
- Households responded vs not, split by side. Non-responders with an email (nudgeable) vs without (call).
- Roll-ups for questions with `show_on_dashboard`: choice counts, or the list of non-empty text answers.
- Recent RSVPs (last 20), new contact submissions, bounced emails, campaign status.
- QR codes as SVG at `/manage/qr/site.svg` (the `?k=` unlock link) and `/manage/qr/album.svg`, generated by
  `qrcode` with its SVG image factory so Pillow is not needed. Print from the browser.
- Server-rendered DaisyUI, no chart library. Linked from the admin index and the staff nav.

**Seating (Phase 5, July 2027).** Reception only, no drag and drop.

- `TableAdmin`: name, capacity, order, notes, and a read-only inline of assigned guests.
- Assignment happens in the `GuestAdmin` changelist: filter to guests attending the reception, pick a
  table in the dropdown for each, save. Ten tables of ten is one screen.
- `/manage/seating/` (`staff_member_required`): one card per table with seats used against capacity and
  its guests grouped by household, meal choice beside each name; an "unassigned" list of attending guests
  grouped by household; and warnings at the top for a table over capacity, a household split across
  tables, a seated guest whose reception `Rsvp` is now `attending=False` or missing, and an attending guest
  with no table. Each warning links to the guest in the admin. `/manage/seating/print/` renders the same
  data one table per page for the venue.
- A declined guest keeps their table and is flagged rather than silently unseated, so a late "actually we
  can come" does not lose the seat.
- Tests: each warning condition fires; the export orders by table then household; a guest with no
  reception RSVP row is unassigned-but-hidden, not a warning, until they respond.

**Match check (Phase 3).** `manage.py rsvp_match_check cases.csv` reads `typed_first, typed_last,
expected_household` rows, runs the matcher, and prints `ok / miss / ambiguous` per row with totals; exits
non-zero on any miss. We seed the CSV with every misspelling and nickname we can think of and run it against
the real list before 2026-12-20.

**Roles.** Two superusers; `GROUP_PERMISSIONS` stays empty. If a parent or planner needs to look, add a
`Planner` group with `guests.view_*` permissions in `core/roles.py` and `sync_groups` handles the rest.

---

## 9. RSVP flow (`guests`, HTMX, Phase 3)

All under `/rsvp/`, no login. Every step works as a plain form post; HTMX only makes it smoother. Live on
production before the first invitation mails, because the card says "RSVP at sarahandfrank.wedding/rsvp/".

**Open or closed.** `rsvp_is_open(event=None)` is `WeddingSettings.rsvp_open` and today (in `TIME_ZONE`) is
on or before `event.rsvp_deadline or WeddingSettings.rsvp_deadline`. When closed, every step renders
`rsvp_closed_message` and, if the session has a household, a read-only summary of their answers. Admin entry
(`source=admin`) is never blocked.

1. **`GET /rsvp/`**: `rsvp_intro`, then first name + last name fields, then "Have a code? Enter it instead."
   A visitor whose session already holds `rsvp_household_id` (from a personalized save-the-date link) skips
   straight to step 4 with a "Not the Smiths? Start over" link.
2. **`POST /rsvp/lookup/`**: runs the matcher (below) or looks up the invite code. Renders candidate
   households as one radio each: "The Smith household (Bob, Carol)". Shows household name and first names
   only, never email or address. At most 3 candidates; their ids go in `session["rsvp_candidates"]`. Zero
   matches: "We couldn't find you. Try the code on your invitation, or email us at <contact_email>." Rate limit
   20 per 10 minutes per IP. logs `rsvp_lookup` with `matched` and `candidate_count`.
3. **`POST /rsvp/select/<household_id>/`**: accepted only if the id is in `rsvp_candidates` or the code
   matched. Sets `session["rsvp_household_id"]`, clears `rsvp_candidates`.
4. **`GET/POST /rsvp/respond/`**: one form built by `guests/forms.py::build_rsvp_form(household)`:
   - For each guest (by `sort_order`) × each invited event with `rsvp_required` (by `Event.order`): a large
     yes / no radio named `attending_<guest_id>_<event_id>`, required. A radio, not a checkbox, so "no" is
     distinguishable from "didn't answer".
   - A plus-one with a blank name gets `plus_one_name_<guest_id>`, required only if they are attending
     anything.
   - Per-guest questions render under each guest as `q_<question_id>_<guest_id>`; per-household questions
     at the end as `q_<question_id>`. Questions with `only_if_attending` start visible and are validated
     server-side: required only when the relevant guest is attending that event, and ignored otherwise.
     Hiding them dynamically via HTMX is a polish item.
   - Contact email prefilled from `household.email`, optional. If blank, the done page says no confirmation
     will be sent.
   - Saved in one `transaction.atomic()`: `update_or_create` each `Rsvp` (`source=guest`, `responded_at=now`),
     each `RsvpAnswer`, plus-one names, `household.responded_at` (first time only), `household.email` (if
     provided). Then `rsvp_confirmation` to the household (if email) and `rsvp_received_internal` to
     `contact_email`. logs `rsvp_submitted` the first time, `rsvp_edited` after.
   - Returning guests see their prior answers prefilled and can edit until the deadline. With invitations
     mailing seven months out, this is not a nicety: expect a steady trickle of edits.
5. **`GET /rsvp/done/`**: summary plus "we emailed a confirmation to <email>". "Not you? Start over" posts to
   `/rsvp/reset/`, which clears the RSVP session keys.

**Emails.** Context dataclasses in `guests/email_contexts.py`; template keys registered in
`messaging/template_definitions.py` (its `_TEMPLATE_DEFINITIONS` mapping is where keys live); default
bodies seeded by a data migration (section 11). `rsvp_confirmation` context: couple names, household name,
per-guest per-event answers, answers to questions, edit URL, deadline, contact email.

**Name matching (`guests/matching.py`).**

1. `normalize(s)`: NFKD, drop combining marks, casefold, replace non-alphanumerics with spaces, collapse
   whitespace, strip.
2. Load every `Guest` with its household once per lookup (≤ 300 rows). Everything below runs in Python, so
   it is database-agnostic and tests run on SQLite.
3. Last-name candidates: any whitespace- or hyphen-separated token of `last_name_normalized` equals the typed
   last name, or `rapidfuzz.fuzz.ratio ≥ 85`. A last-name match is required, which limits enumeration. Use
   `ratio`, not `partial_ratio`, so "Lee" does not match "Leeds".
4. First-name score for each candidate: 100 for exact; 95 for a pair in `guests/nicknames.py` (Robert↔Bob,
   William↔Bill, Elizabeth↔Liz…, about 150 pairs, checked both ways); 95 for a token of `also_known_as`;
   `fuzz.ratio ≥ 80` otherwise; reject below that.
5. Group by household, rank by best score, return at most 3 households.

The invite code path bypasses matching entirely and is what the card tells people to use if lookup fails.

**Privacy.** Lookup reveals that a household with those names exists and its first names. That is
acceptable behind the site password and is what every vendor does. Lookups are logged with IP via structlog.

**Tests.** Table-driven matcher tests (nicknames, accents, typos, hyphenated names, plus-ones with blank
names, the `Lee`/`Leeds` case); the full flow (lookup → select → respond → rows, `responded_at`, two queued
`OutboundEmail`s); a session pre-seeded by a save-the-date link skips lookup; closed and past-deadline
states, including a per-event deadline override; editing preserves prior answers and fires `rsvp_edited`; a
forged household id in `select` is rejected; a conditional question is ignored when the guest declines; the
plus-one name is required only when attending.

---

## 10. Digital save-the-date and address collection (Phase 2a)

One page, two entrances, exempt from the site password. Both render the same save-the-date: the
save-the-date image, "Save the Date", `couple_names`, the date spelled out, `wedding_city`,
`save_the_date_message` ("A formal invitation will follow in the new year. To make sure it reaches you,
please confirm your address below."), an "Add to calendar" link to `/save-the-date/event.ics`, the address
form, and after submitting, a thank-you with a "Visit our website" button.

**Personalized: `GET/POST /save-the-date/<code>/`.** `code` is a `Household.invite_code`; unknown codes 404.
The page greets the household by name ("Bob & Carol") and prefills whatever we already have. Fields: mailing
address (five lines), email, phone, "anything we should know" (goes to `notes`). Submitting writes the
address, email, and phone onto the `Household`, sets `address_confirmed_at`, and logs `address_submitted` with the household id. Viewing sets `site_unlocked` and `rsvp_household_id` in the
session. A "Not Bob & Carol? Use this link instead" line points at the generic page, so a forwarded link does
not overwrite the wrong household. Rate limited at 10 posts per hour per IP. Auditlog records the change
with no actor.

**Generic: `GET/POST /save-the-date/`.** Live while `save_the_date_open`; 404 otherwise. For family group
chats and for anyone who was forwarded a link. Same page, plus a "Your name(s)" field. Submitting creates a
`ContactSubmission` (status `new`), shows the same thank-you, unlocks the site, and logs `address_submitted` without a household. Honeypot field plus the same rate limit; no third-party CAPTCHA
(CSP blocks their scripts anyway).

**Admin review for generic submissions.** `ContactSubmissionAdmin` lists `new` rows with the typed names and
offers "Merge into existing household" (autocomplete; copies address, email, phone; sets
`address_confirmed_at`; marks the submission `merged`) and "Create household from submission" (splits names
on `&`, `and`, `,` into guests). Review is deliberately manual: the free-text names are messy.

**Add to calendar.** `guests/ics.py` renders a single all-day `VEVENT` for `wedding_date` with
`couple_names`, `wedding_city`, and the site URL, served with `Content-Type: text/calendar` and a
`Content-Disposition: attachment` filename. Linked from the save-the-date page and the RSVP confirmation
email.

**Sending it (Freeze 1, 2026-09-30).**

1. By 09-20 the import is done; every household exists by name with whatever email and phone the
   spreadsheet had. Households with neither go to the parents on each side to fill in by 09-27.
2. 09-27: rehearse on staging with `EMAIL_RECIPIENT_ALLOWLIST` set. Open the personalized link from the
   rehearsal email on a phone, submit the form, confirm the household updated. Three relatives try their real
   links on staging and tell us what confused them.
3. 09-30, evening. Set `codes_frozen`. Texts first: download `/manage/export/save-the-date-links.csv` and
   send the prewritten message from both our phones to every household with a phone. Post the generic link in
   the family group chats and ask people to forward it. Then start the `save_the_date` campaign with
   audience `all_with_email` at `daily_cap=25`; for 150 households it finishes in under a week. Texts are the
   primary channel because the sending domain is three weeks old (section 11).
4. Watch the admin's "address confirmed" filter (dashboard from Phase 2b). Nudges (`address_nudge`, audience
   `missing_address`) go out 2026-11-01, 11-22, and 12-10 at `daily_cap=50`; the week of 12-14, whoever is
   left gets a call from whichever side they belong to. 2027-01-05: every household confirmed or entered by
   hand, export for the printer.

**If Phase 2a runs long.** The send needs the personalized page, the `.ics`, and the links export. The
campaign system can follow a week later; until then, households with only an email get a personal email from
our own accounts with their link pasted in. A one-week slip of the send costs one week of address collection
and nothing else. The date that cannot move is 2027-01-05.

**Tests.** Personalized page 404s on an unknown code, greets the right household, prefills, saves, sets
`address_confirmed_at` and the two session keys; generic page 404s when closed, honours the honeypot,
creates a `ContactSubmission`; the `.ics` parses; the merge action copies fields and marks the submission;
a second submission on a personalized link updates rather than duplicates.

---

## 11. Messaging: save-the-date, nudges, confirmations, bounces

**Templates.** Keys in `messaging/template_definitions.py`: `save_the_date`, `address_nudge`,
`rsvp_confirmation`, `rsvp_received_internal`, `rsvp_nudge`, `announcement`. Each has a frozen context
dataclass in `guests/email_contexts.py`. The starter ships no `EmailTemplateVersion` rows and
`EmailTemplateService` raises if one is missing, so `guests/migrations/000X_seed_email_templates.py`
`get_or_create`s one row per key from HTML files in `guests/templates/guests/email/`. They stay editable in
the admin. The pipeline already derives the plain-text alternative with `strip_tags`. Phase 2a seeds
`save_the_date`; the rest arrive with their phases. Every template's footer carries the household's personal
link ("Your personal link, in case you ever need it: …") and `contact_email`, so a guest who loses both the
text and the card can still get in from any email we ever sent them.

The `save_the_date` email is the save-the-date page in miniature: the image by absolute static URL, names,
date, city, one sentence, one big "Confirm your address" button to the personalized link, and a smaller
"Add to calendar" link. Short, one image, no tracking pixels: this is the first bulk send from a young
domain. It does not carry the site password; the link unlocks the site.

**Campaigns (Phase 2a).** `Campaign` + `CampaignAdmin`: pick a template and audience, see the recipient
count and a sample rendering for the first recipient, press "Send".

- Audiences, all excluding households with a blank email, `email_opt_out`, or `email_bounced_at`:
  `all_with_email`; `missing_address` (`address_confirmed_at` null); `non_responders` (`responded_at`
  null); `missing_answers` (any guest lacking an `Rsvp` for an invited `rsvp_required` event); `attending`
  (any guest attending `event`); `declined` (responded, nobody attending anything); `selected` (the
  `households` M2M).
- "Send" sets `status=sending` and enqueues `send_campaign(campaign_id)` on `default`. The task creates
  `CampaignRecipient` rows if none exist (`skipped` for excluded households, with a reason in `error`), then
  takes up to `daily_cap` `queued` recipients and calls `EmailTemplateService.send_email` for each, storing
  the `OutboundEmail` and marking the recipient `sent` (meaning handed to the `email` queue; delivery status
  lives on the `OutboundEmail`). When no `queued` recipients remain, `status=sent`. Rerunning is idempotent.
- `resume_campaigns`, recurring daily at 13:00 UTC (09:00 Eastern in summer), enqueues `send_campaign` for every campaign in `sending`, so
  a 150-household campaign at `daily_cap=25` finishes over six mornings. `steady_queue` evaluates cron in UTC (verified in the package source), hence 13:00 rather than 09:00.
- Template context: couple names, household name, first names, personalized save-the-date URL, RSVP URL,
  invite code, RSVP deadline, site URL, site password (meaningful only after Freeze 2), contact email.
- Per-message pacing is already handled by `EMAIL_RATE_LIMIT_PER_SECOND` and the single-threaded `email`
  worker. logs `campaign_sent` with counts when a campaign reaches `sent`.

**Deliverability.** The sending domain goes live in the first week of September and sends its first
campaign on 09-30. Three weeks is not much aging, so:

- Phase 0a, first: Resend domain verified with the DKIM and SPF records it supplies; DMARC
  `v=DMARC1; p=none; rua=mailto:<contact_email>`; `DEFAULT_FROM_EMAIL=hello@sarahandfrank.wedding` with a
  display name; `DEFAULT_REPLY_TO_EMAIL` a personal address; a test message scoring ≥ 9 on mail-tester.
- Between 09-06 and 09-30, a handful of real emails leave the domain naturally: the superuser verification
  mails, rehearsal sends to our own addresses, a few "does this look right" messages to family. Reply to
  them from the personal Reply-To address so the conversation looks two-way.
- The save-the-date campaign runs at `daily_cap=25` and is the secondary channel; every household with a
  phone gets the text first. Recipients know us and will open and click, which is what warms a domain.
- November nudges at 50 per day; tighten DMARC to `p=quarantine` after them. Check Resend's current
  free-tier limits (100 per day and 3,000 per month at last look) before raising any cap.

**Bounces (Phase 2b, before the first nudge).** Use anymail's tracking webhook rather than writing one.
Mount `path("anymail/", include("anymail.urls"))`; set `ANYMAIL["WEBHOOK_SECRET"]` (random `user:pass`,
embedded in the webhook URL we give Resend) and the Resend signing secret; subscribe the webhook in the
Resend dashboard to `bounced` and `complained`. A receiver in `guests/signals.py` on
`anymail.signals.tracking` sets `email_bounced_at` on every household with that email and logs `email_bounced`. Bounced households show on the dashboard's address list so we text or call instead. Until
the receiver exists, bounces from the September send are read in the Resend dashboard and the household
flagged by hand. Confirm the exact setting names against the installed anymail version.

**SMS: not from the system.** Sending to US numbers through Twilio requires A2P 10DLC registration, which
takes weeks and is not guaranteed for a one-off personal sender. Texts go from our own phones using the
links export; `Household.phone` is captured for that and for calls.

---

## 12. Photos: shared Google Photos album (Phase 4)

Total effort about an hour, most of it in Google Photos.

- Create the album, turn on link sharing and "Collaborate", paste the link into
  `WeddingSettings.photo_album_url`. Anyone with the link can view; anyone with the link and a Google
  account can add photos and videos. Photos others add count against their storage, not ours.
- `/photos/` (site password) shows one big "Open the album" button and `photo_album_note`. The table-card
  QR (`/manage/qr/album.svg`) encodes the album link directly, so nobody types a password at a reception.
- Moderation is after the fact: the owner can remove anything and can turn link sharing off. Set the album
  to hide location data before sharing.
- By **2027-06-01**, a few relatives test the link on their own phones, including one without a Google
  account. If the sign-in friction is bad, book a QR photo vendor (Guesticon, GuestCam, or similar) and
  point the QR there instead. Nothing on our side changes.
- Post-wedding: download the album (Google Takeout or "Download all") to our own drive and keep the link up
  for a year.

---

## 13. Cross-cutting concerns

- **Rate limiting (Phase 1).** `core/ratelimit.py`: a `@ratelimit(rate="10/15m")` view decorator keyed on
  `REMOTE_ADDR` (already the real client IP behind Fly thanks to `TrustedClientIPMiddleware`). Counters are
  fixed-window entries in the shared `DatabaseCache` (`cache.add` then `cache.incr`), so the count is correct
  across gunicorn workers. Over the limit renders a friendly 429 page. No-op when `settings.TESTING`, like
  the existing API throttles. Applied to both save-the-date forms, the password form, and RSVP lookup.
- **Sessions** carry the keys in section 5. Django's defaults (2-week age, `Lax` SameSite, secure cookies
  when `DEBUG=False`) are fine; nothing is set in `settings.py` today.
- **Observability is structlog only.** Domain events (`site_unlocked`, `address_submitted`, `rsvp_lookup`,
  `rsvp_submitted`, `rsvp_edited`, `campaign_sent`, `email_bounced`) are `logger.info` lines with
  low-cardinality keywords, and handled exceptions are `logger.exception`, all readable in the Fly logs.
  No analytics or error-tracking service.
- **Backups (Phase 1).** The guest list is the crown jewel from the day it is imported. `backup_guest_data`,
  recurring nightly at 03:40 (the starter's housekeeping uses 03:00, 03:10, 03:30), runs `dumpdata guests
  pages` and writes `backups/guests-<date>.json` to the private bucket via `default_storage`, keeping 30.
  Restore is `loaddata`. Fly Postgres snapshots sit underneath (check the cluster's retention; it is short
  by default). A manual `pg_dump` on 09-30 before the send, on 2027-01-05 before the printer export, the day
  invitations mail, and the day before the RSVP deadline.
- **Staging** may hold a copy of real guest data (it is ours) but must have `EMAIL_RECIPIENT_ALLOWLIST` set
  and its own bucket. It auto-deploys from `main` after tests today; keep that. Its `min_machines_running =
  0` means the `steady_queue` sidecar only runs while a request has the machine awake, so campaign and
  backup rehearsals on staging need `fly scale count 1` or a manual `send_campaign` run.
- **Release process.** Merging to `main` deploys staging. Production deploys when the release-please PR is
  merged (`feat:` and `fix:` commits create or update it). Before a freeze, merge the release PR and watch
  the deploy finish. In September, expect to cut a release every few days.
- **Custom domain (Phase 0a).** `fly certs add sarahandfrank.wedding` and the DNS records it prints;
  secrets `ALLOWED_HOSTS` (append the domain), `APP_BASE_URL=https://sarahandfrank.wedding` (feeds the CSRF
  fix and every emailed link), `DEFAULT_FROM_EMAIL`, `DEFAULT_REPLY_TO_EMAIL`. Do the same on staging at
  `staging.sarahandfrank.wedding` so the allowlist rehearsal uses real URLs.
- **Capacity.** One shared CPU with 1 GB is plenty: the peaks are the evening the save-the-date texts land
  and the week invitations land, perhaps fifty sessions each, and nothing guest-facing touches the bucket.
  Keep `min_machines_running = 1` in production so the sidecar keeps running.
- **Timezone.** `TIME_ZONE` comes from `TZ` (default `America/New_York`). Anonymous visitors get it from the
  existing `UserTimezoneMiddleware` fallback. `Event.starts_at` is timezone-aware; `rsvp_deadline` is a date
  interpreted in `TIME_ZONE`.
- **Dependencies to add** with `uv add`: `rapidfuzz` (Phase 3), `qrcode` (Phase 2b). No image libraries.
  The `.ics` is a hand-rendered template; no calendar library.
- **Conventions from `CLAUDE.md` that apply everywhere here:** `AppRequest` as the view request type;
  `structlog.get_logger(__name__)` with keyword events; `{% ckeditor_media %}` rather than `{{ form.media }}`
  in our own templates; `.ck-content` + `|safe` for saved rich text; tests in `tests.py`, fixtures from the
  root `conftest.py`, warnings are errors.

---

## 14. Phases, estimates, exit criteria

| # | Phase | Window | Hours | Deliverables | Exit criteria |
|---|---|---|---|---|---|
| 0a | Identity that must age | by 2026-09-06 | 2 | Domain, Fly cert, Resend domain, SPF/DKIM/DMARC, `APP_BASE_URL` and email secrets on both apps, staging subdomain | Production answers at the real domain over HTTPS; a test email from production lands in a Gmail inbox, not spam |
| 1 | Guest data | by 2026-09-20 | 8 | `guests` models + migrations, `HouseholdAdmin` with inline and filters, import resource, `pages.WeddingSettings` + admin, `core/ratelimit.py`, backup task, CSRF-origin fix, staging allowlist + check, `noindex` | The real spreadsheet imports cleanly on staging and then production, and re-imports without duplicates; a backup file appears in the bucket; the cron timezone question is answered |
| 2a | Save-the-date | by 2026-09-27 | 8 | Both save-the-date pages, address form, `.ics`, links export, minimal theme + fonts, placeholder home, `Campaign` + admin + `send_campaign` + `resume_campaigns`, seeded `save_the_date` template, `ContactSubmission` merge actions | End-to-end rehearsal on staging; three relatives open real links on their phones; Lighthouse accessibility ≥ 95 on the save-the-date page |
| — | **Freeze 1** | **2026-09-30** | — | Texts, group-chat link, email campaign at 25 per day | Domain, `/save-the-date/`, and codes constant from here |
| 2b | Site, gate, cleanup | Oct 2026 | 10 | Remove `agreements` + API; `anymail` always installed; full theme + wedding nav; `SitePasswordMiddleware` + `/welcome/`; `Page`, `FaqItem`, home / schedule / registry / FAQ / photos / prose pages; `Event` rows; bounce receiver; `address_nudge` template; dashboard address section; Sarah's account + MFA | Gate works incl. `?k=` on staging with a provisional password and is a no-op on production while the password is blank; a bounce flips `email_bounced_at`; Sarah edits a `Page` and an FAQ unaided; the 11-01 nudge sends from the dashboard's numbers |
| 3 | RSVP | Nov – Dec 2026 | 11 | Matcher, RSVP flow, questions, `rsvp_confirmation` + `rsvp_received_internal` + `rsvp_nudge` + `announcement` templates, dashboard RSVP sections, `rsvps.csv`, `rsvp_match_check` | By 12-20: ten relatives RSVP on their own phones unassisted (staging); match check reports zero misses; Lighthouse ≥ 95 on the RSVP form |
| — | **Freeze 2** | **2027-01-05** | — | Addresses + codes exported for the printer | Site password, `/rsvp/`, and deadline on the invitation proof match production |
| 4 | Photos | Mar 2027 | 1 | Album created and linked; `/photos/`; album QR | Relatives view and add from their phones; go/no-go on a vendor by 2027-06-01 |
| 5 | Run the event | Jan – Sep 2027 | 7 | On the mail date: `site_password` set, `rsvp_open` on, travel and hotel pages published; May `announcement`; July: `Table` + `Guest.table`, `TableAdmin`, `/manage/seating/`, seating export (about 3 hours); August RSVP nudges; caterer export; QR print | Every attending guest seated with no warnings by 09-01; final CSV and seating chart delivered; QR scanned at the venue |
| 6 | Wind-down | Oct 2027 | 2 | `rsvp_open` off; final `dumpdata` + `rsvps.csv`; album downloaded; keep-or-stop decision | Everything exported to our own drive |

Total ≈ 49 hours, 18 of them between now and 2026-09-30. That is the crunch. Section 10 names what the send
actually needs (the personalized page, the `.ics`, the links export) and what can follow it by a week (the
campaign system). October is comfortable. Phase 3 has a hard end because the invitation card names the RSVP
URL; if it slips, the fallback is a card that says "RSVP opens March 1" and a later `rsvp_open`, which is
survivable but worse. If time gets tight later, the dashboard is cut first (the admin changelists carry the
same numbers).

**Implementation status (2026-09-03).** The code for Phases 1, 2a, 2b, 3, 4 and the seating part of 5 is written
and tested (`make test`, `make lint`, `manage.py check`, `makemigrations --check` all green). What remains is
not code:

- Phase 0a by hand: domain, Fly cert, Resend domain, DNS, and the secrets (`APP_BASE_URL`, `ALLOWED_HOSTS`,
  `DEFAULT_FROM_EMAIL`, `DEFAULT_REPLY_TO_EMAIL`, `RESEND_API_KEY`, `ANYMAIL_WEBHOOK_SECRET`,
  `RESEND_SIGNING_SECRET`, and on staging only `EMAIL_RECIPIENT_ALLOWLIST`).
- Content in the admin: `WeddingSettings` (city, message, contact email, later the password and deadline),
  the three `Event` rows, the question set, prose `Page` rows, FAQ items; a real hero image replacing
  `static/img/hero.svg`; optional self-hosted fonts in `static/fonts/` (system stacks are in place).
- The real spreadsheet reshaped to the import columns and imported on staging, then production.
- Sarah's superuser and MFA enrollment; the staging rehearsal with relatives; the Resend webhook subscription.
- Manual testing on phones: the gate, save-the-date pages, RSVP form, dashboard, seating, and the emails.

---

## 15. Decisions log

| # | Decision | Chosen | Date |
|---|---|---|---|
| 1 | Remove `agreements` and the mobile API | Yes, in October; unmount `/api/` now | 2026-09-01, amended 2026-09-03 |
| 2 | Invitation unit | Household; split households for exceptions | 2026-09-01 |
| 3 | Content editing | Sarah edits prose in the admin (`Page`, `FaqItem`, CKEditor) | 2026-09-03 |
| 4 | SMS from the system | No; texts go from our phones using the links export | 2026-09-01, reaffirmed 2026-09-03 |
| 5 | Photos | Shared Google Photos album; vendor as fallback; no build | 2026-09-03 |
| 6 | Gate | One shared site password; save-the-date links unlock without it; password frozen at Freeze 2 | 2026-09-03 |
| 7 | Contact email | One per household, not per guest | 2026-09-01 |
| 8 | Children | `is_child` flag; kids' meal is a choice on the meal question | 2026-09-01 |
| 9 | Events | Ceremony, reception, and a subset-only rehearsal dinner | 2026-09-03 |
| 10 | Domain | `sarahandfrank.wedding`; staging at `staging.sarahandfrank.wedding` | 2026-09-03 |
| 11 | Save-the-date | Digital, by text and email, doubling as the address collector; invitations stay physical | 2026-09-03 |
| 12 | Save-the-date link token | Reuse `Household.invite_code`; codes frozen from the send date | 2026-09-03 |
| 13 | Timeline | Save-the-date 2026-09-30 (one year out); invitations mailed late Jan to mid Feb 2027; print freeze 2027-01-05 | 2026-09-03 |
| 14 | First-send channel | Texts primary, email campaign at 25 per day secondary, because the domain is three weeks old | 2026-09-03 |
| 15 | Seating | Minimal in-system: `Table` + `Guest.table`, dropdown assignment in the admin, `/manage/seating/` with warnings, export; no drag and drop; reception only | 2026-09-03 |
| 16 | Gate timing | Off until the invitations mail; password page points to the invitation and `contact_email`; every guest email footer carries the personal link | 2026-09-03 |
| 17 | Starter features | Removed auditlog, impersonation, roles, registration switch, per-user timezone, profile page, API leftovers, faker seeding; kept staff MFA, structlog | 2026-09-03 |
| 18 | Observability | PostHog and maintenance mode removed; `AppSettings` folded into `WeddingSettings`; structlog is the only telemetry | 2026-09-03 |

---

## 16. Open items (answers needed; the first three matter this week)

1. **Sending address.** Default `hello@sarahandfrank.wedding`. Needed for Phase 0a this week.
2. **The spreadsheet**: approximate household and guest counts, how many households already have an email
   or phone, and its columns, so the import resource is right by 09-20.
3. **Save-the-date content** by 09-25: the image, `wedding_city` wording, the message paragraph, and the
   prewritten text message.
4. **Exact invitation print date** (assumed first week of January) and mail window, so Freeze 2 is a day,
   not a week.
5. **RSVP deadline.** Default 2027-08-14, which is six months after the invitations land. An earlier date
   (say 2027-07-15) gives more time to chase and re-plan, at the cost of asking people to commit earlier.
   Frozen at Freeze 2.
6. **Sarah's email** for the second superuser (October).
7. **Rehearsal dinner details**: date, venue, whether it takes an RSVP, and how we decide who is invited
   (needed for Phase 3).
8. **Site password** (frozen at Freeze 2).
9. **Venue region**, if not Eastern time, for `TZ`.
10. **Content inputs** for October: hero and story photos, story copy, hotel block details, registry links.
11. **Keep or stop after the wedding** (decide in Oct 2027): keep the site as a keepsake with the password
    blanked, or export everything and scale to zero.

---

## 17. Risks

| Risk | Mitigation |
|---|---|
| 18 hours of work in four weeks | Section 10 names the minimum for the send; the campaign can follow by a week; a one-week slip of the send costs a week of address collection and nothing else |
| Three-week-old sending domain lands in spam | DNS live in the first week of September; texts are the primary channel; 25 per day; one image, no trackers; natural low-volume mail beforehand; DMARC reports |
| Guest list not final by 09-20 | Households added later get their own link; the generic link covers anyone forgotten; the list only has to be final by the January print |
| Forwarded personalized link overwrites the wrong household | "Not Bob & Carol? Use this link instead" on the page; auditlog shows the change; the address list makes an odd address visible |
| Households with no email or phone at all | Admin filter lists them; parents on each side fill in contacts or make the call before 09-27 |
| Text blast is manual | Links export with a prewritten message; two phones, one evening; Messages + Shortcuts on a Mac |
| Addresses not all in by 2027-01-05 | Three nudges plus a calling week in December; anything still missing is entered from what parents know |
| RSVP not live when invitations land | Phase 3 ends 12-20 with a two-week buffer; fallback is a card that says "RSVP opens March 1" |
| Seven-month RSVP window: people forget or plans change | Edit-until-deadline; May `announcement`; August nudges; consider an earlier deadline (open item 5) |
| Guest loses the save-the-date text and, later, the card | No wall exists before the invitations mail; afterwards the password page points to the invitation and `contact_email`, and every email footer carries the personal link |
| Late declines after seating is done | The seating page flags a seated guest who declined and an attendee with no seat; check it the week of the wedding before escort cards print |
| Name lookup fails for real people | `rsvp_match_check` against the real list before 12-20; nickname table; invite code on every card as the fallback |
| Sent or printed constants change afterwards | Two explicit freezes; `codes_frozen`; the Freeze 2 checklist compares the printer's proof to production |
| Google Photos sign-in friction | Relatives test by 2027-06-01; vendor fallback needs only a different QR |
| Junk in the shared album | Owner removes; link sharing can be turned off; the album is downloaded post-wedding |
| Single machine | Fly health checks restart it; `entry.sh` supervises the worker; on the day nothing guest-facing depends on the worker |
| Losing the guest list | Nightly `dumpdata` to the bucket from Phase 1; Postgres snapshots; manual dumps before each send and export |
| Test mail reaching real guests from staging | `EMAIL_RECIPIENT_ALLOWLIST` plus a deploy-time check, in Phase 1 before any guest email exists in staging |
| Locked out of the admin (lost phone) | Recovery codes in the shared password manager; two superusers can reset each other |
| Scheduled jobs fire at the wrong hour | Resolved: `steady_queue` runs cron in UTC; schedules are written in UTC with the local time in a comment |
