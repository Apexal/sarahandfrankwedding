"""The contact sheet at /manage/contacts/: every household's contact fields, one cell saved at a time."""

import pytest
from django.urls import reverse
from django.utils import timezone

from guests.models import Guest, Household


@pytest.fixture
def households(db):
    full = Household.objects.create(
        name="The Fulls",
        email="full@example.com",
        phone="(312) 555-0100",
        address_line1="1 Main St",
        city="Chicago",
        state="IL",
        postal_code="60601",
        address_confirmed_at=timezone.now(),
    )
    empty = Household.objects.create(name="The Empties", side=Household.Side.FRANK)
    bounced = Household.objects.create(name="The Bounceds", email="old@example.com", email_bounced_at=timezone.now())
    Guest.objects.create(household=full, first_name="Fran", last_name="Full")
    Guest.objects.create(household=empty, first_name="Emil", last_name="Empty", sort_order=0)
    Guest.objects.create(household=empty, first_name="Ella", last_name="Empty", sort_order=1)
    Guest.objects.create(household=bounced, first_name="Bea", last_name="Bounced")
    return {"full": full, "empty": empty, "bounced": bounced}


def cell_url(household: Household, field: str) -> str:
    return reverse("manage:contact_cell", kwargs={"pk": household.pk, "field": field})


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_is_sent_to_login(self, client):
        response = client.get(reverse("manage:contacts"))
        assert response.status_code == 302
        assert "login" in response.url

    def test_non_staff_cannot_save_a_cell(self, user_client, households):
        response = user_client.post(cell_url(households["empty"], "email"), {"email": "x@example.com"})
        assert response.status_code == 302
        households["empty"].refresh_from_db()
        assert households["empty"].email == ""


class TestSheet:
    def test_lists_every_household_with_its_guests(self, admin_client, households):
        html = admin_client.get(reverse("manage:contacts")).content.decode()
        for household in households.values():
            assert household.name in html
        assert "Emil Empty, Ella Empty" in html
        assert 'value="full@example.com"' in html
        assert "Email 2 / 3" in html
        assert "Confirmed 1 / 3" in html

    @pytest.mark.parametrize(
        ("missing", "expected"),
        [
            ("email", {"The Empties"}),
            ("phone", {"The Empties", "The Bounceds"}),
            ("address", {"The Empties", "The Bounceds"}),
            ("unconfirmed", {"The Empties", "The Bounceds"}),
            ("any", {"The Empties", "The Bounceds"}),
            ("", {"The Fulls", "The Empties", "The Bounceds"}),
        ],
    )
    def test_missing_filter(self, admin_client, households, missing, expected):
        response = admin_client.get(
            reverse("manage:contacts"), {"missing": missing}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="contact-rows"
        )
        html = response.content.decode()
        assert 'id="contact-rows"' in html
        shown = {household.name for household in households.values() if household.name in html}
        assert shown == expected

    def test_search_and_side(self, admin_client, households):
        response = admin_client.get(
            reverse("manage:contacts"),
            {"q": "ella", "side": "frank"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="contact-rows",
        )
        html = response.content.decode()
        assert "The Empties" in html
        assert "The Fulls" not in html

    def test_summary_partial(self, admin_client, households):
        response = admin_client.get(
            reverse("manage:contacts"), HTTP_HX_REQUEST="true", HTTP_HX_TARGET="contacts-summary"
        )
        html = response.content.decode()
        assert 'id="contacts-summary"' in html
        assert "contact-rows" not in html


class TestCell:
    def test_saves_one_field_and_signals_the_summary(self, admin_client, households):
        empty = households["empty"]
        response = admin_client.post(cell_url(empty, "city"), {"city": "  Evanston "}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert 'value="Evanston"' in response.content.decode()
        assert "input-success" in response.content.decode()
        assert "contacts-changed" in response["HX-Trigger-After-Swap"]
        empty.refresh_from_db()
        assert empty.city == "Evanston"

    def test_phone_is_normalized(self, admin_client, households):
        empty = households["empty"]
        response = admin_client.post(cell_url(empty, "phone"), {"phone": "(773) 555-0199"})
        assert 'value="+17735550199"' in response.content.decode()
        empty.refresh_from_db()
        assert empty.phone == "+17735550199"

    def test_phone_with_no_digits_is_rejected(self, admin_client, households):
        empty = households["empty"]
        response = admin_client.post(cell_url(empty, "phone"), {"phone": "call mom"})
        html = response.content.decode()
        assert "Enter a phone number." in html
        assert 'value="call mom"' in html
        assert "HX-Trigger-After-Swap" not in response
        empty.refresh_from_db()
        assert empty.phone == ""

    def test_invalid_email_keeps_what_was_typed(self, admin_client, households):
        full = households["full"]
        response = admin_client.post(cell_url(full, "email"), {"email": "not-an-email"})
        html = response.content.decode()
        assert 'value="not-an-email"' in html
        assert "input-error" in html
        full.refresh_from_db()
        assert full.email == "full@example.com"

    def test_blanking_a_field(self, admin_client, households):
        full = households["full"]
        admin_client.post(cell_url(full, "address_line1"), {"address_line1": ""})
        full.refresh_from_db()
        assert full.address_line1 == ""

    def test_changing_a_bounced_email_clears_the_bounce(self, admin_client, households):
        bounced = households["bounced"]
        admin_client.post(cell_url(bounced, "email"), {"email": "new@example.com"})
        bounced.refresh_from_db()
        assert bounced.email == "new@example.com"
        assert bounced.email_bounced_at is None

    def test_resaving_a_bounced_email_keeps_the_bounce(self, admin_client, households):
        bounced = households["bounced"]
        response = admin_client.post(cell_url(bounced, "email"), {"email": "OLD@example.com"})
        assert ">Bounced</p>" in response.content.decode()
        bounced.refresh_from_db()
        assert bounced.email_bounced_at is not None

    def test_address_confirmed_checkbox(self, admin_client, households):
        empty, full = households["empty"], households["full"]
        admin_client.post(cell_url(empty, "address_confirmed"), {"address_confirmed": "on"})
        admin_client.post(cell_url(full, "address_confirmed"), {})  # unticked sends nothing
        empty.refresh_from_db()
        full.refresh_from_db()
        assert empty.address_confirmed_at is not None
        assert full.address_confirmed_at is None

    def test_ticking_an_already_confirmed_address_keeps_its_date(self, admin_client, households):
        full = households["full"]
        when = full.address_confirmed_at
        admin_client.post(cell_url(full, "address_confirmed"), {"address_confirmed": "on"})
        full.refresh_from_db()
        assert full.address_confirmed_at == when

    @pytest.mark.parametrize("field", ["name", "invite_code", "notes", "email_bounced_at"])
    def test_other_fields_are_not_editable(self, admin_client, households, field):
        response = admin_client.post(cell_url(households["full"], field), {field: "X"})
        assert response.status_code == 404

    def test_get_is_not_allowed(self, admin_client, households):
        assert admin_client.get(cell_url(households["full"], "email")).status_code == 405
