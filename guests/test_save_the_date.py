"""The digital save-the-date: personalized and generic pages, and the calendar file."""

from datetime import UTC, date, datetime

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from guests.ics import escape_text, fold, ics_filename, render_ics
from guests.models import ContactSubmission, Guest, Household
from guests.session import RSVP_HOUSEHOLD_SESSION_KEY
from pages.middleware import SESSION_UNLOCK_KEY
from pages.models import WeddingSettings

PASSWORD = "Willow2027"
GENERIC = reverse("save_the_date:generic")
ICS = reverse("save_the_date:ics")


def configure(**fields) -> WeddingSettings:
    wedding = WeddingSettings.load()
    for name, value in fields.items():
        setattr(wedding, name, value)
    wedding.save()
    return wedding


def address_payload(**overrides) -> dict[str, str]:
    payload = {
        "address_line1": "12 Willow Lane",
        "address_line2": "Apt 4",
        "city": "Evanston",
        "state": "IL",
        "postal_code": "60201",
        "email": "bob@example.com",
        "phone": "(312) 555-0100",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def wedding(db):
    return configure(
        couple_names="Sarah & Frank",
        wedding_date=date(2027, 9, 18),
        wedding_city="Chicago, Illinois",
        contact_email="hello@example.com",
        save_the_date_message="<p>Please <strong>confirm</strong> your address.</p>",
    )


@pytest.fixture
def household(wedding):
    household = Household.objects.create(
        name="The Joneses",
        email="jones@example.com",
        address_line1="1 Old Road",
        city="Chicago",
        state="IL",
        postal_code="60601",
        notes="Imported from the spreadsheet.",
    )
    Guest.objects.create(household=household, first_name="Bob", last_name="Jones")
    Guest.objects.create(household=household, first_name="Carol", last_name="Jones", sort_order=1)
    return household


@pytest.fixture
def personalized_url(household):
    return reverse("save_the_date:personalized", kwargs={"code": household.invite_code})


@pytest.mark.django_db
class TestPersonalizedPage:
    def test_unknown_code_404s(self, client, wedding):
        assert client.get(reverse("save_the_date:personalized", kwargs={"code": "NOPE42"})).status_code == 404

    def test_get_greets_prefills_and_sets_both_session_keys(self, client, household, personalized_url):
        response = client.get(personalized_url)
        assert response.status_code == 200
        html = response.content.decode()
        assert "Hi Bob &amp; Carol!" in html
        assert "Sarah &amp; Frank" in html
        assert '<time datetime="2027-09-18">Saturday,' in html
        assert "September 18, 2027" in html
        assert "Chicago, Illinois" in html
        assert '<p class="env-address-names">See you soon?</p>' in html  # the envelope's sign-off, not their names
        assert (
            '<div class="ck-content mb-2 text-center text-small leading-relaxed"><p>Please <strong>confirm</strong> your address.</p></div>'
            in html
        )
        assert f'href="{ICS}"' in html
        assert 'value="1 Old Road"' in html
        assert 'value="jones@example.com"' in html
        assert 'name="names"' not in html  # only the generic page asks for names
        assert client.session[RSVP_HOUSEHOLD_SESSION_KEY] == household.pk
        assert client.session[SESSION_UNLOCK_KEY] == WeddingSettings.load().site_password_fingerprint

    def test_reachable_and_unlocks_the_site_while_gated(self, client, household, personalized_url):
        configure(site_password=PASSWORD)
        assert client.get("/").status_code == 302  # locked before the visit
        assert client.get(personalized_url).status_code == 200
        assert client.session[SESSION_UNLOCK_KEY] == WeddingSettings.load().site_password_fingerprint
        assert client.get("/").status_code == 200

    def test_code_is_case_insensitive(self, client, household):
        url = reverse("save_the_date:personalized", kwargs={"code": household.invite_code.lower()})
        assert client.get(url).status_code == 200

    def test_every_input_has_a_label(self, client, household, personalized_url):
        html = client.get(personalized_url).content.decode()
        for name in ("address_line1", "address_line2", "city", "state", "postal_code", "email", "phone"):
            assert f'<label for="id_{name}"' in html, name
            assert f'id="id_{name}"' in html, name
        assert "id_country" not in html  # not asked; the household keeps its own
        assert "id_note" not in html
        assert '<button type="submit" class="button button-primary mt-2 w-full">Confirm address</button>' in html

    def test_post_saves_confirms_and_appends_the_note(self, client, household, personalized_url):
        before = timezone.now()
        response = client.post(personalized_url, address_payload())
        assert response.status_code == 200
        html = response.content.decode()
        assert "Thank you, Bob &amp; Carol!" in html
        assert f'href="{reverse("pages:home")}"' in html
        assert "Visit our website" in html
        assert "<form" not in html

        household.refresh_from_db()
        assert household.address_line1 == "12 Willow Lane"
        assert household.address_line2 == "Apt 4"
        assert household.city == "Evanston"
        assert household.state == "IL"
        assert household.postal_code == "60201"
        assert household.country == "US"
        assert household.email == "bob@example.com"
        assert household.phone == "+13125550100"  # normalized
        assert household.address_confirmed_at is not None
        assert household.address_confirmed_at >= before
        assert household.notes == "Imported from the spreadsheet."
        assert household.name == "The Joneses"

    def test_second_post_updates_the_same_household(self, client, household, personalized_url):
        client.post(personalized_url, address_payload())
        first_confirmed_at = Household.objects.get(pk=household.pk).address_confirmed_at
        response = client.post(personalized_url, address_payload(address_line1="99 New Street"))
        assert response.status_code == 200
        assert Household.objects.count() == 1
        household.refresh_from_db()
        assert household.address_line1 == "99 New Street"
        assert household.address_confirmed_at >= first_confirmed_at

    def test_invalid_post_rerenders_with_an_error_summary(self, client, household, personalized_url):
        response = client.post(personalized_url, address_payload(address_line1="", email="not-an-email"))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="form-errors"' in html
        assert 'href="#id_address_line1"' in html
        assert 'href="#id_email"' in html
        assert 'aria-invalid="true"' in html
        assert 'id="id_email_error"' in html
        assert "Thank you" not in html
        household.refresh_from_db()
        assert household.address_confirmed_at is None
        assert household.email == "jones@example.com"

    def test_htmx_post_returns_only_the_thank_you(self, client, household, personalized_url):
        response = client.post(personalized_url, address_payload(), headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.content.decode()
        assert html.lstrip().startswith('<div id="std-reply">')
        assert "Thank you, Bob &amp; Carol!" in html
        assert "<html" not in html
        assert "lettering" not in html  # the photo panel stays put and is not redrawn
        household.refresh_from_db()
        assert household.address_confirmed_at is not None

    def test_htmx_post_with_errors_returns_only_the_form(self, client, household, personalized_url):
        response = client.post(personalized_url, address_payload(address_line1=""), headers={"HX-Request": "true"})
        html = response.content.decode()
        assert html.lstrip().startswith('<div id="std-reply">')
        assert 'id="form-errors"' in html
        assert 'hx-target="#std-reply"' in html
        assert "<html" not in html

    def test_envelope_is_sealed_on_arrival_and_open_after_a_post(self, client, household, personalized_url):
        html = client.get(personalized_url).content.decode()
        assert 'class="std-stage is-sealed"' in html
        assert "js/save-the-date.js" in html
        # A re-render with errors, or the thank-you, must not make the guest open it again.
        html = client.post(personalized_url, address_payload(address_line1="")).content.decode()
        assert 'class="std-stage is-done"' in html
        html = client.post(personalized_url, address_payload()).content.decode()
        assert 'class="std-stage is-done"' in html

    def test_unusable_phone_is_rejected(self, client, household, personalized_url):
        response = client.post(personalized_url, address_payload(phone="call me"))
        assert "Enter a phone number with digits" in response.content.decode()
        household.refresh_from_db()
        assert household.address_confirmed_at is None

    @pytest.mark.parametrize("state", ["", "Illinois", "ZZ"])
    def test_state_must_be_one_of_the_listed_codes(self, client, household, personalized_url, state):
        response = client.post(personalized_url, address_payload(state=state))
        assert 'id="id_state_error"' in response.content.decode()
        household.refresh_from_db()
        assert household.address_confirmed_at is None

    @pytest.mark.parametrize(("stored", "selected"), [("IL", "IL"), ("illinois", "IL"), ("Ontario", "")])
    def test_state_dropdown_preselects_a_stored_state(self, client, household, personalized_url, stored, selected):
        household.state = stored
        household.save()
        html = client.get(personalized_url).content.decode()
        assert '<select name="state"' in html
        if selected:
            assert f'<option value="{selected}" selected>' in html
        else:
            assert '<option value="" selected>Select a state</option>' in html

    def test_not_you_line_points_at_the_generic_page_when_open(self, client, household, personalized_url):
        html = client.get(personalized_url).content.decode()
        assert "Not Bob &amp; Carol?" in html
        assert f'href="{GENERIC}"' not in html
        assert 'href="mailto:hello@example.com"' in html
        configure(save_the_date_open=True)
        html = client.get(personalized_url).content.decode()
        assert f'href="{GENERIC}"' in html
        assert "Use this link instead" in html

    def test_default_message_when_none_is_configured(self, client, household, personalized_url):
        configure(save_the_date_message="")
        html = client.get(personalized_url).content.decode()
        assert "A formal invitation will follow in the new year." in html
        assert "ck-content" not in html

    @override_settings(TESTING=False)
    def test_posts_are_rate_limited_per_ip(self, client, household, personalized_url):
        for _ in range(10):
            assert client.post(personalized_url, address_payload()).status_code == 200
        assert client.post(personalized_url, address_payload()).status_code == 429
        assert client.get(personalized_url).status_code == 200  # GETs never burn the budget


@pytest.mark.django_db
class TestLetterPages:
    """The `letter/` twins: the same pages opened straight to the letter, for working on its content."""

    def test_personalized_letter_opens_without_the_envelope(self, client, household, wedding):
        configure(site_password=PASSWORD)
        url = reverse("save_the_date:personalized_letter", kwargs={"code": household.invite_code})
        response = client.get(url)
        assert response.status_code == 200  # exempt from the password like its twin
        html = response.content.decode()
        assert 'class="std-stage is-done"' in html
        assert "env-hit" not in html
        assert "Hi Bob &amp; Carol!" in html
        assert 'value="1 Old Road"' in html
        assert f'<form method="post" action="{url}" hx-post="{url}"' in html
        assert client.session[RSVP_HOUSEHOLD_SESSION_KEY] == household.pk

    def test_personalized_letter_saves_like_its_twin(self, client, household):
        url = reverse("save_the_date:personalized_letter", kwargs={"code": household.invite_code})
        response = client.post(url, address_payload())
        assert "Thank you, Bob &amp; Carol!" in response.content.decode()
        household.refresh_from_db()
        assert household.address_confirmed_at is not None

    def test_generic_letter_follows_the_generic_switch(self, client, wedding):
        url = reverse("save_the_date:generic_letter")
        assert url == "/save-the-date/letter/"
        assert client.get(url).status_code == 404
        configure(save_the_date_open=True)
        html = client.get(url).content.decode()
        assert 'class="std-stage is-done"' in html
        assert 'name="names"' in html
        assert "env-hit" not in html


@pytest.mark.django_db
class TestGenericPage:
    def test_404_while_closed(self, client, wedding):
        assert client.get(GENERIC).status_code == 404
        assert client.post(GENERIC, address_payload(names="Bob & Carol Jones")).status_code == 404
        assert ContactSubmission.objects.count() == 0

    def test_200_and_unlocks_while_open(self, client, wedding):
        configure(save_the_date_open=True, site_password=PASSWORD)
        response = client.get(GENERIC)
        assert response.status_code == 200
        html = response.content.decode()
        assert 'name="names"' in html
        assert '<label for="id_names"' in html
        assert 'name="website"' in html  # the honeypot
        assert "<div hidden" in html
        assert "Hi " not in html
        assert client.session[SESSION_UNLOCK_KEY] == WeddingSettings.load().site_password_fingerprint
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session
        assert client.get("/").status_code == 200

    def test_post_creates_a_contact_submission_with_the_ip(self, client, wedding):
        configure(save_the_date_open=True)
        response = client.post(GENERIC, address_payload(names="Bob & Carol Jones"), REMOTE_ADDR="203.0.113.7")
        assert response.status_code == 200
        html = response.content.decode()
        assert "Thank you!" in html
        assert f'href="{reverse("pages:home")}"' in html
        assert "<form" not in html

        submission = ContactSubmission.objects.get()
        assert submission.names == "Bob & Carol Jones"
        assert submission.status == ContactSubmission.Status.NEW
        assert submission.ip == "203.0.113.7"
        assert submission.address_line1 == "12 Willow Lane"
        assert submission.city == "Evanston"
        assert submission.postal_code == "60201"
        assert submission.email == "bob@example.com"
        assert submission.phone == "+13125550100"
        assert submission.notes == ""
        assert submission.household is None
        assert Household.objects.count() == 0

    def test_honeypot_returns_the_thank_you_and_saves_nothing(self, client, wedding):
        configure(save_the_date_open=True)
        response = client.post(GENERIC, address_payload(names="Bot", website="https://spam.example"))
        assert response.status_code == 200
        assert "Thank you!" in response.content.decode()
        assert ContactSubmission.objects.count() == 0

    def test_names_are_required(self, client, wedding):
        configure(save_the_date_open=True)
        response = client.post(GENERIC, address_payload(names=""))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="form-errors"' in html
        assert 'href="#id_names"' in html
        assert ContactSubmission.objects.count() == 0

    @override_settings(TESTING=False)
    def test_posts_are_rate_limited_per_ip(self, client, wedding):
        configure(save_the_date_open=True)
        for _ in range(10):
            assert client.post(GENERIC, address_payload(names="Bob")).status_code == 200
        assert client.post(GENERIC, address_payload(names="Bob")).status_code == 429
        assert ContactSubmission.objects.count() == 10


@pytest.mark.django_db
class TestCalendarFile:
    def test_serves_an_all_day_event_as_an_attachment(self, client, wedding):
        response = client.get(ICS)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/calendar; charset=utf-8"
        assert response["Content-Disposition"] == 'attachment; filename="sarah-and-frank-wedding.ics"'
        body = response.content.decode()
        assert body.startswith("BEGIN:VCALENDAR\r\n")
        assert body.endswith("END:VCALENDAR\r\n")
        assert "\n" not in body.replace("\r\n", "")  # CRLF everywhere, never a bare LF
        assert "BEGIN:VEVENT\r\n" in body
        assert "DTSTART;VALUE=DATE:20270918\r\n" in body
        assert "DTEND;VALUE=DATE:20270919\r\n" in body
        assert "SUMMARY:Sarah & Frank's wedding\r\n" in body
        assert "LOCATION:Chicago\\, Illinois\r\n" in body
        assert "UID:wedding@" in body
        assert "DTSTAMP:" in body
        assert "URL:http" in body

    def test_reachable_while_gated(self, client, wedding):
        configure(site_password=PASSWORD)
        assert client.get(ICS).status_code == 200

    def test_render_is_stable_and_carries_the_site_url(self, wedding):
        now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
        body = render_ics(wedding, site_url="https://sarahandfrank.wedding/", now=now)
        assert "UID:wedding@sarahandfrank.wedding\r\n" in body
        assert "DTSTAMP:20260930T180000Z\r\n" in body
        assert "URL:https://sarahandfrank.wedding\r\n" in body
        unfolded = body.replace("\r\n ", "")
        assert "September 18\\, 2027 in Chicago\\, Illinois. Details: https://sarahandfrank.wedding" in unfolded
        assert body == render_ics(wedding, site_url="https://sarahandfrank.wedding", now=now)

    def test_omits_the_location_without_a_city(self, wedding):
        configure(wedding_city="")
        assert "LOCATION:" not in render_ics(WeddingSettings.load())

    def test_long_lines_are_folded_at_75_octets(self):
        folded = fold("DESCRIPTION:" + "x" * 200)
        for line in folded.split("\r\n"):
            assert len(line.encode()) <= 75
        assert folded.replace("\r\n ", "") == "DESCRIPTION:" + "x" * 200
        # Multi-byte characters are never split.
        folded = fold("SUMMARY:" + "é" * 100)
        assert folded.replace("\r\n ", "") == "SUMMARY:" + "é" * 100
        for line in folded.split("\r\n"):
            assert len(line.encode()) <= 75

    def test_text_escaping(self):
        assert escape_text("a;b,c\\d\nnext") == "a\\;b\\,c\\\\d\\nnext"

    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            ("Sarah & Frank", "sarah-and-frank-wedding.ics"),
            ("Sarah and Frank", "sarah-and-frank-wedding.ics"),
            ("  ", "wedding.ics"),
        ],
    )
    def test_filename_is_derived_from_the_couple_names(self, names, expected):
        assert ics_filename(names) == expected
