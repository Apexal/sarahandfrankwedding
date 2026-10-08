from datetime import date, timedelta

import pytest
import tablib
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    INVITE_CODE_ALPHABET,
    INVITE_CODE_LENGTH,
    Campaign,
    CampaignRecipient,
    CodesFrozenError,
    ContactSubmission,
    Event,
    Guest,
    Household,
    Rsvp,
    RsvpAnswer,
    RsvpQuestion,
    Table,
    normalize_name,
    split_names,
)
from guests.resources import GuestResource
from pages.models import WeddingSettings


@pytest.fixture
def events(db):
    now = timezone.now()
    return {
        "ceremony": Event.objects.create(name="Ceremony", slug="ceremony", starts_at=now, order=1),
        "reception": Event.objects.create(name="Reception", slug="reception", starts_at=now, order=2),
        "rehearsal": Event.objects.create(
            name="Rehearsal dinner", slug="rehearsal", starts_at=now - timedelta(days=1), is_private=True, order=0
        ),
    }


@pytest.fixture
def household(events):
    household = Household.objects.create(name="The Joneses", email="jones@example.com", phone="(312) 555-0100")
    household.invited_events.set([events["ceremony"], events["reception"]])
    Guest.objects.create(household=household, first_name="Robert", last_name="Jones", also_known_as="Bob, Bobby")
    Guest.objects.create(household=household, first_name="Carol", last_name="Jones", sort_order=1)
    return household


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Robert", "robert"),
            ("  ROBERT  ", "robert"),
            ("José", "jose"),
            ("Zoë Smith-Jones", "zoe smith jones"),
            ("O'Brien", "o brien"),
            ("Straße", "strasse"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize_name(raw) == expected


class TestSplitNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Bob & Carol Jones", [("Bob", "Jones"), ("Carol", "Jones")]),
            ("Bob and Carol Jones", [("Bob", "Jones"), ("Carol", "Jones")]),
            ("Bob Jones and Carol Smith", [("Bob", "Jones"), ("Carol", "Smith")]),
            ("Bob Jones, Carol Jones, Timmy", [("Bob", "Jones"), ("Carol", "Jones"), ("Timmy", "")]),
            ("Mary Ann Van Der Berg", [("Mary", "Ann Van Der Berg")]),
            ("", []),
        ],
    )
    def test_splits(self, raw, expected):
        assert split_names(raw) == expected


@pytest.mark.django_db
class TestHousehold:
    def test_invite_code_is_generated_from_the_safe_alphabet(self):
        household = Household.objects.create(name="A")
        assert len(household.invite_code) == INVITE_CODE_LENGTH
        assert set(household.invite_code) <= set(INVITE_CODE_ALPHABET)
        for banned in "0O1IL":
            assert banned not in INVITE_CODE_ALPHABET

    def test_invite_code_survives_resave_and_is_unique(self):
        first = Household.objects.create(name="A")
        code = first.invite_code
        first.name = "A2"
        first.save()
        assert first.invite_code == code
        codes = {Household.objects.create(name=f"H{i}").invite_code for i in range(50)}
        assert len(codes) == 50

    def test_regenerate_changes_the_code_unless_frozen(self):
        household = Household.objects.create(name="A")
        old = household.invite_code
        assert household.regenerate_invite_code() != old
        settings = WeddingSettings.load()
        settings.codes_frozen = True
        settings.save()
        with pytest.raises(CodesFrozenError):
            household.regenerate_invite_code()

    def test_phone_is_normalized(self):
        household = Household.objects.create(name="A", phone="(312) 555-0100")
        assert household.phone == "+13125550100"

    def test_address_state_is_the_timestamp_not_the_fields(self):
        household = Household.objects.create(name="A", address_line1="1 Main St", city="Chicago", postal_code="60601")
        assert household.has_address
        assert not household.address_confirmed
        household.mark_address_confirmed()
        assert household.address_confirmed

    def test_save_the_date_url_uses_app_base_url(self, settings):
        settings.APP_BASE_URL = "https://sarahandfrank.wedding/"
        household = Household.objects.create(name="A")
        assert household.save_the_date_url == f"https://sarahandfrank.wedding/save-the-date/{household.invite_code}/"

    def test_contact_names_skips_plus_ones(self, household):
        Guest.objects.create(household=household, is_plus_one=True, sort_order=2)
        assert household.contact_names == "Robert & Carol"
        assert Household.objects.create(name="Empty").contact_names == "Empty"

    def test_can_email(self):
        assert not Household(name="A").can_email
        assert Household(name="A", email="a@example.com").can_email
        assert not Household(name="A", email="a@example.com", email_opt_out=True).can_email
        assert not Household(name="A", email="a@example.com", email_bounced_at=timezone.now()).can_email


@pytest.mark.django_db
class TestGuest:
    def test_normalized_names_set_on_save(self, household):
        guest = Guest.objects.create(household=household, first_name="José", last_name="Núñez-Smith")
        assert guest.first_name_normalized == "jose"
        assert guest.last_name_normalized == "nunez smith"

    def test_display_name_for_plus_one(self, household):
        plus_one = Guest(household=household, is_plus_one=True)
        assert plus_one.display_name == "Guest of The Joneses"
        assert str(household.guests.first()) == "Robert Jones"

    def test_named_guest_requires_a_name(self, household):
        with pytest.raises(ValidationError):
            Guest(household=household).full_clean()
        Guest(household=household, is_plus_one=True).full_clean()

    def test_aliases(self, household):
        assert household.guests.get(first_name="Robert").aliases == ["bob", "bobby"]

    def test_table_assignment_is_cleared_when_the_table_goes(self, household):
        table = Table.objects.create(name="Table 1", capacity=8)
        guest = household.guests.first()
        guest.table = table
        guest.save()
        assert table.seats_used == 1 and table.seats_free == 7
        table.delete()
        guest.refresh_from_db()
        assert guest.table is None


@pytest.mark.django_db
class TestEvent:
    def test_cannot_end_before_it_starts(self):
        now = timezone.now()
        with pytest.raises(ValidationError):
            Event(name="X", slug="x", starts_at=now, ends_at=now - timedelta(hours=1)).full_clean()

    def test_effective_deadline_falls_back_to_wedding_settings(self, events):
        settings = WeddingSettings.load()
        settings.rsvp_deadline = date(2027, 8, 14)
        settings.save()
        assert events["ceremony"].effective_rsvp_deadline == date(2027, 8, 14)
        events["ceremony"].rsvp_deadline = date(2027, 7, 1)
        assert events["ceremony"].effective_rsvp_deadline == date(2027, 7, 1)


@pytest.mark.django_db
class TestRsvp:
    def test_one_row_per_guest_and_event(self, household, events):
        guest = household.guests.first()
        Rsvp.objects.create(guest=guest, event=events["ceremony"], attending=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            Rsvp.objects.create(guest=guest, event=events["ceremony"], attending=False)
        Rsvp.objects.create(guest=guest, event=events["reception"], attending=False)


@pytest.mark.django_db
class TestRsvpQuestion:
    def test_choice_kinds_need_choices(self):
        question = RsvpQuestion(label="Meal", kind=RsvpQuestion.Kind.SINGLE_CHOICE, choices=["Beef"])
        with pytest.raises(ValidationError):
            question.full_clean()
        question.choices = ["Beef", "Beef"]
        with pytest.raises(ValidationError):
            question.full_clean()
        question.choices = ["Beef", "Fish"]
        question.full_clean()

    def test_text_kinds_reject_choices(self):
        question = RsvpQuestion(label="Diet", kind=RsvpQuestion.Kind.SHORT_TEXT, choices=["a", "b"])
        with pytest.raises(ValidationError):
            question.full_clean()


@pytest.mark.django_db
class TestRsvpAnswer:
    @pytest.fixture
    def questions(self, events):
        return {
            "meal": RsvpQuestion.objects.create(
                label="Meal",
                kind=RsvpQuestion.Kind.SINGLE_CHOICE,
                choices=["Beef", "Fish"],
                scope=RsvpQuestion.Scope.PER_GUEST,
                only_if_attending=events["reception"],
            ),
            "song": RsvpQuestion.objects.create(
                label="Song", kind=RsvpQuestion.Kind.SHORT_TEXT, scope=RsvpQuestion.Scope.PER_HOUSEHOLD
            ),
        }

    def test_scope_decides_whether_guest_is_set(self, household, questions):
        guest = household.guests.first()
        with pytest.raises(ValidationError):
            RsvpAnswer(question=questions["song"], household=household, guest=guest, value="x").full_clean()
        with pytest.raises(ValidationError):
            RsvpAnswer(question=questions["meal"], household=household, value="Beef").full_clean()
        RsvpAnswer(question=questions["meal"], household=household, guest=guest, value="Beef").full_clean()
        RsvpAnswer(question=questions["song"], household=household, value="x").full_clean()

    def test_guest_must_belong_to_the_household(self, household, questions):
        other = Household.objects.create(name="Other")
        stranger = Guest.objects.create(household=other, first_name="S", last_name="T")
        with pytest.raises(ValidationError):
            RsvpAnswer(question=questions["meal"], household=household, guest=stranger, value="Beef").full_clean()

    def test_one_answer_per_household_for_household_questions(self, household, questions):
        RsvpAnswer.objects.create(question=questions["song"], household=household, value="a")
        with pytest.raises(IntegrityError), transaction.atomic():
            RsvpAnswer.objects.create(question=questions["song"], household=household, value="b")

    def test_one_answer_per_guest_for_guest_questions(self, household, questions):
        robert, carol = household.guests.all()
        RsvpAnswer.objects.create(question=questions["meal"], household=household, guest=robert, value="Beef")
        RsvpAnswer.objects.create(question=questions["meal"], household=household, guest=carol, value="Fish")
        with pytest.raises(IntegrityError), transaction.atomic():
            RsvpAnswer.objects.create(question=questions["meal"], household=household, guest=robert, value="Fish")


@pytest.mark.django_db
class TestContactSubmission:
    def test_merge_into_copies_details_and_confirms(self, household):
        submission = ContactSubmission.objects.create(
            names="Bob & Carol Jones",
            email="new@example.com",
            phone="312-555-0199",
            address_line1="2 Oak St",
            city="Evanston",
            state="IL",
            postal_code="60201",
            notes="Second floor",
        )
        submission.merge_into(household)
        household.refresh_from_db()
        assert household.address_line1 == "2 Oak St"
        assert household.address_confirmed
        assert household.email == "new@example.com"
        assert household.phone == "+13125550199"
        assert "Second floor" in household.notes
        submission.refresh_from_db()
        assert submission.status == ContactSubmission.Status.MERGED
        assert submission.household == household

    def test_merge_without_address_does_not_confirm(self, household):
        ContactSubmission.objects.create(names="Bob", email="b@example.com").merge_into(household)
        household.refresh_from_db()
        assert not household.address_confirmed
        assert household.email == "b@example.com"


@pytest.mark.django_db
class TestCampaign:
    def test_attending_requires_an_event(self):
        with pytest.raises(ValidationError):
            Campaign(name="x", audience=Campaign.Audience.ATTENDING).full_clean()

    def test_sending_requires_a_template(self):
        Campaign(name="x", audience=Campaign.Audience.ALL_WITH_EMAIL).full_clean()
        with pytest.raises(ValidationError):
            Campaign(name="x", audience=Campaign.Audience.ALL_WITH_EMAIL, status=Campaign.Status.SENDING).full_clean()

    def test_audiences(self, household, events):
        bounced = Household.objects.create(name="Bounced", email="b@example.com", email_bounced_at=timezone.now())
        opted_out = Household.objects.create(name="Out", email="o@example.com", email_opt_out=True)
        no_email = Household.objects.create(name="NoEmail")
        confirmed = Household.objects.create(
            name="Confirmed", email="c@example.com", address_confirmed_at=timezone.now()
        )
        confirmed.invited_events.set([events["reception"]])
        confirmed_guest = Guest.objects.create(household=confirmed, first_name="C", last_name="D")
        Rsvp.objects.create(guest=confirmed_guest, event=events["reception"], attending=False)
        confirmed.responded_at = timezone.now()
        confirmed.save()
        robert = household.guests.get(first_name="Robert")
        Rsvp.objects.create(guest=robert, event=events["reception"], attending=True)

        def audience(kind, **extra):
            return set(Campaign(name="x", audience=kind, **extra).audience_households().values_list("name", flat=True))

        assert audience(Campaign.Audience.ALL_WITH_EMAIL) == {"The Joneses", "Confirmed"}
        assert audience(Campaign.Audience.MISSING_ADDRESS) == {"The Joneses"}
        assert audience(Campaign.Audience.NON_RESPONDERS) == {"The Joneses"}
        # The Joneses: Carol has no reception row and nobody answered the ceremony. Confirmed answered everything.
        assert audience(Campaign.Audience.MISSING_ANSWERS) == {"The Joneses"}
        assert audience(Campaign.Audience.ATTENDING, event=events["reception"]) == {"The Joneses"}
        assert audience(Campaign.Audience.ATTENDING) == set()
        assert audience(Campaign.Audience.DECLINED) == {"Confirmed"}
        campaign = Campaign.objects.create(name="sel", audience=Campaign.Audience.SELECTED)
        campaign.households.set([household, bounced, opted_out, no_email])
        assert set(campaign.audience_households().values_list("name", flat=True)) == {"The Joneses"}

    def test_recipient_is_unique_per_campaign(self, household):
        campaign = Campaign.objects.create(name="x", audience=Campaign.Audience.ALL_WITH_EMAIL)
        CampaignRecipient.objects.create(campaign=campaign, household=household)
        with pytest.raises(IntegrityError), transaction.atomic():
            CampaignRecipient.objects.create(campaign=campaign, household=household)


@pytest.mark.django_db
class TestGuestImport:
    HEADERS = [
        "household",
        "side",
        "household_email",
        "household_phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
        "events",
        "first_name",
        "last_name",
        "is_plus_one",
        "is_child",
        "also_known_as",
        "guest_email",
        "notes",
    ]

    def _dataset(self, rows):
        dataset = tablib.Dataset(headers=self.HEADERS)
        for row in rows:
            dataset.append(row)
        return dataset

    def _rows(self):
        return [
            [
                "The Smiths",
                "sarah",
                "smiths@example.com",
                "312-555-0101",
                "1 Main St",
                "",
                "Chicago",
                "IL",
                "60601",
                "US",
                "ceremony;reception",
                "Bob",
                "Smith",
                "",
                "",
                "Robert",
                "",
                "Old friends",
            ],
            [
                "The Smiths",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "Carol",
                "Smith",
                "",
                "",
                "",
                "carol@example.com",
                "",
            ],
            ["The Smiths", "", "", "", "", "", "", "", "", "", "", "", "", "yes", "", "", "", ""],
            ["Tim Lee & Guest", "frank", "", "", "", "", "", "", "", "", "reception", "Tim", "Lee", "", "", "", "", ""],
            ["Tim Lee & Guest", "", "", "", "", "", "", "", "", "", "", "", "", "x", "", "", "", ""],
        ]

    def test_dry_run_creates_nothing(self, events):
        result = GuestResource().import_data(self._dataset(self._rows()), dry_run=True)
        assert not result.has_errors(), result.row_errors()
        assert not Household.objects.exists()
        assert not Guest.objects.exists()

    def test_import_creates_households_guests_and_events(self, events):
        result = GuestResource().import_data(self._dataset(self._rows()), dry_run=False)
        assert not result.has_errors(), result.row_errors()
        smiths = Household.objects.get(name="The Smiths")
        assert smiths.side == "sarah"
        assert smiths.email == "smiths@example.com"
        assert smiths.phone == "+13125550101"
        assert smiths.city == "Chicago"
        assert smiths.notes == "Old friends"
        assert not smiths.address_confirmed
        assert set(smiths.invited_events.values_list("slug", flat=True)) == {"ceremony", "reception"}
        assert smiths.guests.count() == 3
        bob = smiths.guests.get(first_name="Bob")
        assert bob.also_known_as == "Robert"
        assert smiths.guests.filter(is_plus_one=True, first_name="").exists()
        lee = Household.objects.get(name="Tim Lee & Guest")
        assert list(lee.invited_events.values_list("slug", flat=True)) == ["reception"]
        assert lee.guests.filter(is_plus_one=True).count() == 1

    def test_reimport_updates_instead_of_duplicating(self, events):
        rows = self._rows()
        GuestResource().import_data(self._dataset(rows), dry_run=False)
        rows[0][15] = "Robert, Bobby"
        result = GuestResource().import_data(self._dataset(rows), dry_run=False)
        assert not result.has_errors(), result.row_errors()
        assert Household.objects.count() == 2
        assert Guest.objects.count() == 5
        assert Guest.objects.get(household__name="The Smiths", first_name="Bob").also_known_as == "Robert, Bobby"

    def test_unknown_event_slug_is_a_row_error(self, events):
        rows = self._rows()
        rows[0][10] = "ceremony;brunch"
        result = GuestResource().import_data(self._dataset(rows), dry_run=True)
        assert result.has_errors()
        assert "brunch" in str(result.row_errors()[0][1][0].error)

    def test_export_round_trips_household_columns(self, household):
        dataset = GuestResource().export()
        assert dataset.headers[:3] == ["household", "side", "household_email"]
        first = dict(zip(dataset.headers, dataset[0], strict=True))
        assert first["household"] == "The Joneses"
        assert first["events"] == "ceremony;reception"
        assert first["household_email"] == "jones@example.com"


@pytest.mark.django_db
class TestAdmin:
    @pytest.mark.parametrize(
        "url_name",
        [
            "admin:guests_household_changelist",
            "admin:guests_guest_changelist",
            "admin:guests_event_changelist",
            "admin:guests_table_changelist",
            "admin:guests_rsvp_changelist",
            "admin:guests_rsvpquestion_changelist",
            "admin:guests_rsvpanswer_changelist",
            "admin:guests_contactsubmission_changelist",
            "admin:guests_campaign_changelist",
            "admin:guests_campaignrecipient_changelist",
        ],
    )
    def test_changelists_load(self, admin_client, household, events, url_name):
        robert = household.guests.first()
        Rsvp.objects.create(guest=robert, event=events["ceremony"], attending=True)
        question = RsvpQuestion.objects.create(label="Song", kind="short_text", scope="per_household")
        RsvpAnswer.objects.create(question=question, household=household, value="x")
        Table.objects.create(name="Table 1", capacity=8)
        ContactSubmission.objects.create(names="Someone")
        campaign = Campaign.objects.create(name="c", audience=Campaign.Audience.ALL_WITH_EMAIL)
        CampaignRecipient.objects.create(campaign=campaign, household=household)
        response = admin_client.get(reverse(url_name))
        assert response.status_code == 200

    def test_guest_changelist_has_a_column_per_event(self, admin_client, household, events):
        robert = household.guests.first()
        Rsvp.objects.create(guest=robert, event=events["ceremony"], attending=True)
        html = admin_client.get(reverse("admin:guests_guest_changelist")).content.decode()
        assert "Ceremony" in html and "Reception" in html and "Rehearsal dinner" in html
        assert 'alt="True"' in html
        assert 'alt="None"' in html  # pending renders as the "unknown" icon

    def test_household_change_page_shows_link_and_inline(self, admin_client, household):
        html = admin_client.get(reverse("admin:guests_household_change", args=[household.pk])).content.decode()
        assert household.save_the_date_url in html
        assert "Robert" in html

    def _action(self, admin_client, url_name, action, pks, **extra):
        return admin_client.post(
            reverse(url_name), {"action": action, "_selected_action": [str(pk) for pk in pks], **extra}, follow=True
        )

    def test_regenerate_action_respects_the_freeze(self, admin_client, household):
        old = household.invite_code
        response = self._action(
            admin_client, "admin:guests_household_changelist", "regenerate_invite_codes", [household.pk]
        )
        assert response.status_code == 200
        household.refresh_from_db()
        assert household.invite_code != old
        settings = WeddingSettings.load()
        settings.codes_frozen = True
        settings.save()
        frozen = household.invite_code
        response = self._action(
            admin_client, "admin:guests_household_changelist", "regenerate_invite_codes", [household.pk]
        )
        assert "frozen" in response.content.decode()
        household.refresh_from_db()
        assert household.invite_code == frozen

    def test_mark_address_confirmed_action(self, admin_client, household):
        response = self._action(
            admin_client, "admin:guests_household_changelist", "mark_address_confirmed", [household.pk]
        )
        assert "no address" in response.content.decode()
        household.address_line1, household.city, household.postal_code = "1 Main", "Chicago", "60601"
        household.save()
        self._action(admin_client, "admin:guests_household_changelist", "mark_address_confirmed", [household.pk])
        household.refresh_from_db()
        assert household.address_confirmed

    def test_csv_exports(self, admin_client, household):
        settings = WeddingSettings.load()
        settings.wedding_city = "Chicago, Illinois"
        settings.save()
        response = admin_client.post(
            reverse("admin:guests_household_changelist"),
            {"action": "export_invite_codes_csv", "_selected_action": [household.pk]},
        )
        assert response["Content-Type"].startswith("text/csv")
        body = response.content.decode("utf-8-sig")
        assert body.splitlines()[0].startswith("household,invite_code")
        assert household.invite_code in body

        response = admin_client.post(
            reverse("admin:guests_household_changelist"),
            {"action": "export_save_the_date_links_csv", "_selected_action": [household.pk]},
        )
        body = response.content.decode("utf-8-sig")
        assert household.save_the_date_url in body
        assert "Hi Robert & Carol!" in body
        assert "September 18, 2027 in Chicago, Illinois" in body

    def test_send_campaign_action_creates_a_draft(self, admin_client, household):
        response = admin_client.post(
            reverse("admin:guests_household_changelist"),
            {"action": "send_campaign_to_selected", "_selected_action": [household.pk]},
        )
        campaign = Campaign.objects.get()
        assert response.status_code == 302
        assert response.url == reverse("admin:guests_campaign_change", args=[campaign.pk])
        assert campaign.audience == Campaign.Audience.SELECTED
        assert campaign.status == Campaign.Status.DRAFT
        assert list(campaign.households.all()) == [household]
        assert campaign.created_by is not None
        assert admin_client.get(response.url).status_code == 200

    def test_contact_submission_actions(self, admin_client, household):
        fresh = ContactSubmission.objects.create(
            names="Ann & Dan Park",
            address_line1="9 Elm",
            city="Oak Park",
            postal_code="60302",
            email="park@example.com",
        )
        linked = ContactSubmission.objects.create(names="Jones update", household=household, phone="312-555-0150")
        unlinked = ContactSubmission.objects.create(names="Nobody")
        self._action(
            admin_client, "admin:guests_contactsubmission_changelist", "create_household_from_submission", [fresh.pk]
        )
        park = Household.objects.get(name="Ann & Dan Park")
        assert [(g.first_name, g.last_name) for g in park.guests.all()] == [("Ann", "Park"), ("Dan", "Park")]
        assert park.address_confirmed and park.email == "park@example.com"
        fresh.refresh_from_db()
        assert fresh.status == ContactSubmission.Status.MERGED and fresh.household == park

        response = self._action(
            admin_client,
            "admin:guests_contactsubmission_changelist",
            "merge_into_linked_household",
            [linked.pk, unlinked.pk],
        )
        assert "no household linked" in response.content.decode()
        household.refresh_from_db()
        assert household.phone == "+13125550150"
        linked.refresh_from_db()
        assert linked.status == ContactSubmission.Status.MERGED

        self._action(admin_client, "admin:guests_contactsubmission_changelist", "mark_ignored", [unlinked.pk])
        unlinked.refresh_from_db()
        assert unlinked.status == ContactSubmission.Status.IGNORED

    def test_rsvp_saved_in_admin_is_marked_admin_sourced(self, admin_client, household, events):
        robert = household.guests.first()
        response = admin_client.post(
            reverse("admin:guests_rsvp_add"),
            {
                "guest": robert.pk,
                "event": events["ceremony"].pk,
                "attending": "on",
                "responded_at_0": "2027-08-01",
                "responded_at_1": "12:00:00",
            },
        )
        assert response.status_code == 302, response.content
        assert Rsvp.objects.get().source == Rsvp.Source.ADMIN

    def test_guest_import_page_loads(self, admin_client):
        assert admin_client.get(reverse("admin:guests_guest_import")).status_code == 200
        assert admin_client.get(reverse("admin:guests_household_export")).status_code == 200
