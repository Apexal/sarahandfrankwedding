"""The staff dashboard, seating chart, CSV exports, and QR codes under /manage/."""

import csv
import io
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    Campaign,
    CampaignRecipient,
    ContactSubmission,
    Event,
    Guest,
    Household,
    Rsvp,
    RsvpAnswer,
    RsvpQuestion,
    Table,
)
from guests.views.manage import site_qr_url
from pages.models import WeddingSettings

MANAGE_URLS = [
    ("manage:dashboard", {}),
    ("manage:seating", {}),
    ("manage:seating_print", {}),
    ("manage:export_rsvps", {}),
    ("manage:export_seating", {}),
    ("manage:export_save_the_date_links", {}),
    ("manage:export_invite_codes", {}),
    ("manage:qr", {"name": "site"}),
]


def _csv_rows(response) -> list[list[str]]:
    assert response["Content-Type"].startswith("text/csv")
    assert response.content.startswith("﻿".encode())
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.fixture
def data(db):
    """Two events; three households (confirmed address, missing address, bounced email); some RSVPs."""
    now = timezone.now()
    ceremony = Event.objects.create(name="Ceremony", slug="ceremony", starts_at=now, order=1)
    reception = Event.objects.create(name="Reception", slug="reception", starts_at=now, order=2)

    confirmed = Household.objects.create(
        name="The Confirmeds",
        side=Household.Side.SARAH,
        email="confirmed@example.com",
        address_confirmed_at=now,
        responded_at=now,
    )
    missing = Household.objects.create(
        name="The Missings", side=Household.Side.FRANK, email="missing@example.com", phone="(312) 555-0100"
    )
    bounced = Household.objects.create(
        name="The Bounceds", side=Household.Side.FRANK, email="bounced@example.com", email_bounced_at=now
    )
    for household in (confirmed, missing, bounced):
        household.invited_events.set([ceremony, reception])

    alice = Guest.objects.create(household=confirmed, first_name="Alice", last_name="Confirmed", sort_order=0)
    bob = Guest.objects.create(household=confirmed, first_name="Bob", last_name="Confirmed", sort_order=1)
    carol = Guest.objects.create(household=missing, first_name="Carol", last_name="Missing")
    dan = Guest.objects.create(household=bounced, first_name="Dan", last_name="Bounced", sort_order=0)
    eve = Guest.objects.create(household=bounced, first_name="Eve", last_name="Bounced", sort_order=1, is_child=True)

    Rsvp.objects.create(guest=alice, event=ceremony, attending=True)
    Rsvp.objects.create(guest=alice, event=reception, attending=True)
    Rsvp.objects.create(guest=bob, event=ceremony, attending=False)
    Rsvp.objects.create(guest=bob, event=reception, attending=True)

    meal = RsvpQuestion.objects.create(
        label="Meal choice",
        kind=RsvpQuestion.Kind.SINGLE_CHOICE,
        choices=["Beef", "Fish", "Vegetarian"],
        scope=RsvpQuestion.Scope.PER_GUEST,
        show_on_dashboard=True,
        order=0,
    )
    dietary = RsvpQuestion.objects.create(
        label="Dietary restrictions", kind=RsvpQuestion.Kind.SHORT_TEXT, scope=RsvpQuestion.Scope.PER_GUEST, order=1
    )
    anything = RsvpQuestion.objects.create(
        label="Anything else?",
        kind=RsvpQuestion.Kind.LONG_TEXT,
        scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
        show_on_dashboard=True,
        order=2,
    )
    RsvpAnswer.objects.create(question=meal, household=confirmed, guest=alice, value="Beef")
    RsvpAnswer.objects.create(question=meal, household=confirmed, guest=bob, value="Fish")
    RsvpAnswer.objects.create(question=dietary, household=confirmed, guest=alice, value="No nuts")
    RsvpAnswer.objects.create(question=anything, household=confirmed, value="We're bringing a cake")

    ContactSubmission.objects.create(names="Someone New")
    campaign = Campaign.objects.create(
        name="Save the date", audience=Campaign.Audience.ALL_WITH_EMAIL, status=Campaign.Status.SENDING
    )
    CampaignRecipient.objects.create(campaign=campaign, household=missing, status=CampaignRecipient.Status.SENT)
    CampaignRecipient.objects.create(campaign=campaign, household=confirmed, status=CampaignRecipient.Status.QUEUED)

    return SimpleNamespace(
        now=now,
        ceremony=ceremony,
        reception=reception,
        confirmed=confirmed,
        missing=missing,
        bounced=bounced,
        alice=alice,
        bob=bob,
        carol=carol,
        dan=dan,
        eve=eve,
        campaign=campaign,
    )


@pytest.fixture
def seated(data):
    """Two tables with every warning condition and one guest who must trigger none."""
    table1 = Table.objects.create(name="Table 1", capacity=2, order=1)
    table2 = Table.objects.create(name="Table 2", capacity=4, order=2)
    extras = Household.objects.create(name="The Extras", side=Household.Side.BOTH)
    extras.invited_events.set([data.reception])
    gina = Guest.objects.create(household=extras, first_name="Gina", last_name="Extra", sort_order=0)
    hal = Guest.objects.create(household=extras, first_name="Hal", last_name="Extra", sort_order=1)

    Rsvp.objects.create(guest=gina, event=data.reception, attending=True)
    Rsvp.objects.create(guest=data.carol, event=data.reception, attending=True)  # attending, no table
    Rsvp.objects.create(guest=data.dan, event=data.reception, attending=False)  # declined, keeps the seat
    # Hal is seated with no reception RSVP; Eve has no RSVP and no seat.

    Guest.objects.filter(pk__in=[data.alice.pk, data.dan.pk, gina.pk, hal.pk]).update(table=table1)  # 4 in 2 seats
    Guest.objects.filter(pk=data.bob.pk).update(table=table2)  # splits The Confirmeds
    data.table1, data.table2, data.gina, data.hal = table1, table2, gina, hal
    return data


@pytest.mark.django_db
class TestAccess:
    @pytest.mark.parametrize(("url_name", "kwargs"), MANAGE_URLS)
    def test_anonymous_is_sent_to_login(self, client, url_name, kwargs):
        response = client.get(reverse(url_name, kwargs=kwargs))
        assert response.status_code == 302
        assert "login" in response.url

    @pytest.mark.parametrize(("url_name", "kwargs"), MANAGE_URLS)
    def test_non_staff_is_sent_to_login(self, user_client, url_name, kwargs):
        response = user_client.get(reverse(url_name, kwargs=kwargs))
        assert response.status_code == 302
        assert "login" in response.url


@pytest.mark.django_db
class TestDashboard:
    def test_renders_with_counts(self, admin_client, data):
        response = admin_client.get(reverse("manage:dashboard"))
        assert response.status_code == 200
        html = response.content.decode()
        assert "Dashboard" in html

        events = {row["event"].slug: row for row in response.context["events"]}
        ceremony, reception = events["ceremony"], events["reception"]
        assert (ceremony["invited"], ceremony["yes"], ceremony["no"], ceremony["pending"]) == (5, 1, 1, 3)
        assert (reception["invited"], reception["yes"], reception["no"], reception["pending"]) == (5, 2, 0, 3)
        assert ceremony["progress"]["percent"] == 40

        address = response.context["address"]
        by_label = {row["label"]: (row["done"], row["total"]) for row in address["progress"]}
        assert by_label["All households"] == (1, 3)
        assert by_label["Sarah's side"] == (1, 1)
        assert by_label["Frank's side"] == (0, 2)
        assert by_label["Both"] == (0, 0)

        responses = response.context["responses"]
        assert {row["label"]: row["done"] for row in responses["progress"]}["All households"] == 1
        assert responses["non_responders"] == 2
        assert responses["nudgeable"] == 1  # The Missings
        assert responses["call"] == 1  # The Bounceds cannot be emailed

        assert response.context["new_submissions"] == 1
        assert reverse("admin:guests_contactsubmission_changelist") in response.context["submissions_url"]
        assert [household.pk for household in response.context["bounced"]] == [data.bounced.pk]
        assert len(response.context["recent_rsvps"]) == 4

    def test_missing_address_list(self, admin_client, data):
        response = admin_client.get(reverse("manage:dashboard"))
        missing = {household.pk: household for household in response.context["address"]["missing"]}
        assert set(missing) == {data.missing.pk, data.bounced.pk}
        assert missing[data.missing.pk].emailed is True
        assert missing[data.bounced.pk].emailed is False
        html = response.content.decode()
        assert data.missing.save_the_date_url in html
        assert reverse("admin:guests_household_change", args=[data.missing.pk]) in html
        assert "+13125550100" in html

    def test_question_rollups(self, admin_client, data):
        response = admin_client.get(reverse("manage:dashboard"))
        rollups = {item["question"].label: item for item in response.context["questions"]}
        assert set(rollups) == {"Meal choice", "Anything else?"}  # the dietary question is not on the dashboard
        meal = rollups["Meal choice"]
        assert meal["kind"] == "choices"
        assert [(row["label"], row["done"]) for row in meal["rows"]] == [("Beef", 1), ("Fish", 1), ("Vegetarian", 0)]
        anything = rollups["Anything else?"]
        assert anything["kind"] == "text"
        assert anything["answers"] == [{"who": "The Confirmeds", "text": "We're bringing a cake"}]

    def test_campaign_summary(self, admin_client, data):
        response = admin_client.get(reverse("manage:dashboard"))
        (item,) = response.context["campaigns"]
        assert item["campaign"] == data.campaign
        assert item["counts"] == [("Queued", 1), ("Handed to the email queue", 1)]
        html = response.content.decode()
        assert reverse("admin:guests_campaign_change", args=[data.campaign.pk]) in html

    def test_links_to_exports_and_qr(self, admin_client, data):
        wedding = WeddingSettings.load()
        wedding.photo_album_url = "https://photos.app.goo.gl/abc"
        wedding.save()
        html = admin_client.get(reverse("manage:dashboard")).content.decode()
        for name in ("export_rsvps", "export_seating", "export_save_the_date_links", "export_invite_codes"):
            assert reverse(f"manage:{name}") in html
        assert reverse("manage:qr", kwargs={"name": "site"}) in html
        assert reverse("manage:qr", kwargs={"name": "album"}) in html

    def test_renders_empty(self, admin_client):
        assert admin_client.get(reverse("manage:dashboard")).status_code == 200

    def test_query_count_is_bounded(self, admin_client, seated, django_assert_max_num_queries):
        # Every section is annotated or prefetched; nothing here should grow with the guest list.
        # The first request also bootstraps the settings singletons, so measure the second.
        admin_client.get(reverse("manage:dashboard"))
        with django_assert_max_num_queries(35):
            admin_client.get(reverse("manage:dashboard"))


@pytest.mark.django_db
class TestQr:
    def test_site_url_uses_the_unlock_link_only_with_a_password(self, settings):
        settings.APP_BASE_URL = "https://sarahandfrank.wedding/"
        wedding = WeddingSettings.load()
        assert site_qr_url(wedding) == "https://sarahandfrank.wedding/"
        wedding.site_password = "Sept2027"
        assert site_qr_url(wedding) == "https://sarahandfrank.wedding/welcome/?k=Sept2027"

    def test_site_svg(self, admin_client):
        wedding = WeddingSettings.load()
        wedding.site_password = "Sept2027"
        wedding.save()
        response = admin_client.get(reverse("manage:qr", kwargs={"name": "site"}))
        assert response.status_code == 200
        assert response["Content-Type"] == "image/svg+xml"
        assert response.content.startswith(b"<svg")

    def test_album_svg_needs_an_album(self, admin_client):
        url = reverse("manage:qr", kwargs={"name": "album"})
        assert admin_client.get(url).status_code == 404
        wedding = WeddingSettings.load()
        wedding.photo_album_url = "https://photos.app.goo.gl/abc"
        wedding.save()
        response = admin_client.get(url)
        assert response.status_code == 200
        assert response["Content-Type"] == "image/svg+xml"

    def test_unknown_name_404s(self, admin_client):
        assert admin_client.get(reverse("manage:qr", kwargs={"name": "other"})).status_code == 404


@pytest.mark.django_db
class TestExports:
    def test_rsvps_csv_pivots_per_guest(self, admin_client, data):
        header, *rows = _csv_rows(admin_client.get(reverse("manage:export_rsvps")))
        assert header == [
            "household",
            "side",
            "guest",
            "is_child",
            "attending:ceremony",
            "attending:reception",
            "q:Meal choice",
            "q:Dietary restrictions",
            "q:Anything else?",
            "household_email",
            "responded_at",
        ]
        assert [row[2] for row in rows] == [
            "Dan Bounced",
            "Eve Bounced",
            "Alice Confirmed",
            "Bob Confirmed",
            "Carol Missing",
        ]
        by_guest = {row[2]: row for row in rows}
        assert by_guest["Alice Confirmed"] == [
            "The Confirmeds",
            "sarah",
            "Alice Confirmed",
            "no",
            "yes",
            "yes",
            "Beef",
            "No nuts",
            "We're bringing a cake",
            "confirmed@example.com",
            data.now.isoformat(),
        ]
        bob = by_guest["Bob Confirmed"]
        assert bob[4:9] == ["no", "yes", "Fish", "", "We're bringing a cake"]  # household answer repeated
        carol = by_guest["Carol Missing"]
        assert carol[4:6] == ["", ""]  # pending is blank
        assert carol[10] == ""
        assert by_guest["Eve Bounced"][3] == "yes"

    def test_seating_csv_orders_by_table_then_household(self, admin_client, seated):
        header, *rows = _csv_rows(admin_client.get(reverse("manage:export_seating")))
        assert header == ["table", "guest", "household", "is_child", "meal", "dietary"]
        assert rows == [
            ["Table 1", "Dan Bounced", "The Bounceds", "no", "", ""],
            ["Table 1", "Alice Confirmed", "The Confirmeds", "no", "Beef", "No nuts"],
            ["Table 1", "Gina Extra", "The Extras", "no", "", ""],
            ["Table 1", "Hal Extra", "The Extras", "no", "", ""],
            ["Table 2", "Bob Confirmed", "The Confirmeds", "no", "Fish", ""],
            ["", "Carol Missing", "The Missings", "no", "", ""],
        ]

    def test_save_the_date_links_and_invite_codes(self, admin_client, data):
        header, *rows = _csv_rows(admin_client.get(reverse("manage:export_save_the_date_links")))
        assert header == ["household", "contact_names", "phone", "email", "link", "message"]
        assert [row[0] for row in rows] == ["The Bounceds", "The Confirmeds", "The Missings"]
        assert rows[2][4] == data.missing.save_the_date_url

        header, *rows = _csv_rows(admin_client.get(reverse("manage:export_invite_codes")))
        assert header[:2] == ["household", "invite_code"]
        assert {row[1] for row in rows} == {
            data.confirmed.invite_code,
            data.missing.invite_code,
            data.bounced.invite_code,
        }


@pytest.mark.django_db
class TestSeating:
    def test_warnings(self, admin_client, seated):
        response = admin_client.get(reverse("manage:seating"))
        assert response.status_code == 200
        warnings = response.context["warnings"]
        by_kind: dict[str, list[dict]] = {}
        for warning in warnings:
            by_kind.setdefault(warning["kind"], []).append(warning)

        (over,) = by_kind["over_capacity"]
        assert "Table 1" in over["message"]
        assert over["url"] == reverse("admin:guests_table_change", args=[seated.table1.pk])

        (split,) = by_kind["split_household"]
        assert split["message"].startswith("The Confirmeds is split across Table 1, Table 2")
        assert reverse("admin:guests_guest_changelist") in split["url"]

        not_attending = {warning["url"]: warning["message"] for warning in by_kind["seated_not_attending"]}
        assert "declined" in not_attending[reverse("admin:guests_guest_change", args=[seated.dan.pk])]
        assert "not RSVPed" in not_attending[reverse("admin:guests_guest_change", args=[seated.hal.pk])]

        (unseated,) = by_kind["unseated"]
        assert unseated["url"] == reverse("admin:guests_guest_change", args=[seated.carol.pk])
        assert "Carol Missing" in unseated["message"]

        assert not any("Eve" in warning["message"] for warning in warnings)

    def test_cards_and_unassigned(self, admin_client, seated):
        response = admin_client.get(reverse("manage:seating"))
        cards = {card["table"].name: card for card in response.context["cards"]}
        table1 = cards["Table 1"]
        assert (table1["used"], table1["over"]) == (4, True)
        assert [group["household"].name for group in table1["households"]] == [
            "The Bounceds",
            "The Confirmeds",
            "The Extras",
        ]
        seats = {seat["guest"].pk: seat for group in table1["households"] for seat in group["guests"]}
        assert seats[seated.dan.pk]["status"] == "declined"  # flagged, still seated
        assert seats[seated.alice.pk]["meal"] == "Beef"
        assert cards["Table 2"]["over"] is False

        unassigned = [seat["guest"].pk for group in response.context["unassigned"] for seat in group["guests"]]
        assert unassigned == [seated.carol.pk]  # Eve has no RSVP row, so she is not unassigned yet
        assert response.context["unassigned_count"] == 1
        assert f"attending={seated.reception.pk}" in response.context["assign_url"]

        html = response.content.decode()
        assert "Declined" in html
        assert reverse("admin:guests_guest_change", args=[seated.carol.pk]) in html

    def test_no_rsvp_event_means_no_rsvp_warnings(self, admin_client, seated):
        Event.objects.all().delete()
        response = admin_client.get(reverse("manage:seating"))
        assert response.status_code == 200
        assert response.context["reception"] is None
        assert {warning["kind"] for warning in response.context["warnings"]} == {"over_capacity", "split_household"}
        assert response.context["unassigned"] == []

    def test_print_view(self, admin_client, seated):
        response = admin_client.get(reverse("manage:seating_print"))
        assert response.status_code == 200
        html = response.content.decode()
        assert "Table 1" in html and "Table 2" in html
        assert "Alice Confirmed" in html
        assert "Carol Missing" not in html  # unassigned guests are not on the venue's copy
        assert "break-after-page" in html

    def test_query_count_is_bounded(self, admin_client, seated, django_assert_max_num_queries):
        admin_client.get(reverse("manage:seating"))
        with django_assert_max_num_queries(15):
            admin_client.get(reverse("manage:seating"))

    def test_empty_seating(self, admin_client):
        assert admin_client.get(reverse("manage:seating")).status_code == 200
        assert admin_client.get(reverse("manage:seating_print")).status_code == 200
