# guests

Guest list system of record. The public flows (save-the-date, RSVP), the dashboard, and campaign sending
live in later slices; `PLAN.md` sections 6 and 8 to 11 are the spec. Today: models, import/export, admin,
and the guest list editor.

- **The unit of invitation is the household; the unit of RSVP is (guest, event).** Per-guest invitation
  exceptions are handled by splitting a household in two, never by a per-guest override.
- **Pending is the absence of a row.** No `Rsvp` for (guest, event) means not answered. Counts are
  `invited_events × guests` minus rows; `Campaign.Audience.MISSING_ANSWERS` exists for the case where an
  event is added after a household responded.
- **`Household.invite_code`** is generated in `save()` from an alphabet without 0/O/1/I/L. It is the token
  in the save-the-date link and the code on the invitation. `regenerate_invite_code()` raises
  `CodesFrozenError` once `WeddingSettings.codes_frozen` is on; the admin action checks the same flag.
- **Address state is `address_confirmed_at`**, not whether the address fields are blank. Imports never set
  it; the save-the-date form and the "mark confirmed" action do. `ContactSubmission.merge_into()` is the one
  merge path, used by the admin actions.
- **Normalized names** are set in `Guest.save()`; the RSVP matcher reads them. Put any typed input through
  `normalize_name()` before comparing.
- **`RsvpAnswer.guest`** is null iff the question is per-household. Two partial unique constraints enforce
  one answer per (question, household[, guest]) identically on SQLite and Postgres.
- **`Campaign.audience_households()`** is the audience definition; the send task records per-recipient
  skips on top of it. `template_key` may be blank on a draft; `clean()` requires it before sending.
- **The guest list editor** (`/manage/guests/`) puts every write through `guests/guest_list.py`: household
  order is `Household.sort_order` (new = 0, top of the list; reorders renumber from 1 and keep filtered-out
  households in their slots), guest order is `Guest.sort_order`. `move_guest()` and `set_invitation()`
  delete RSVPs for events the household is no longer invited to; views ask `rsvps_lost_by_move()` /
  `rsvps_lost_by_uninvite()` first and show a confirm. Empty households are kept. Deletes with something to
  lose (`household_stakes()` / `guest_stakes()`) need the name typed. Confirmations render into the page's
  `<dialog>` via `HX-Retarget`, never a browser dialog; JS is `static/js/guest-list.js` (CSP: no inline).
- **Seating is reception-only.** `Guest.table` is one assignment per guest. The editor at `/manage/seating/`
  puts every write through `guests/seating.py` and answers with the whole board (`seating/_board.html`)
  re-rendered, so counts and warnings never drift. Capacity is warned about, never enforced. The pool is
  reception-invited guests with no table who have not declined. JS is `static/js/seating.js`; it reuses the
  guest list editor's `#gl-modal` dialog and error banner from `static/js/guest-list.js`.
- **Import** is `guests.resources.GuestResource`: one row per guest, household columns repeated, first row
  wins the household fields, `events` is semicolon-separated slugs that must already exist. Guests match on
  (household, first_name, last_name).
- This app imports `pages.models.WeddingSettings`. `pages` must never import `guests.models` at module level.
