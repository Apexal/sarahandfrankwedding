"""The staff pages for sending the save-the-date, reviewing submissions, campaigns, and email templates."""

from urllib.parse import unquote

import pytest
from django.urls import reverse
from django.utils import timezone
from steady_queue.models import Job

from guests.campaigns import retry_failed, start_campaign
from guests.email_preview import TEST_SUBJECT_PREFIX
from guests.models import Campaign, CampaignRecipient, ContactSubmission, Event, Guest, Household
from guests.submissions import SubmissionError, create_household, suggest_households
from messaging.models import EmailTemplateVersion, OutboundEmail
from pages.models import WeddingSettings

SEND_CAMPAIGN_TASK = "guests.tasks.send_campaign"
SEND_EMAIL_TASK = "messaging.tasks.send_outbound_email_task"

STAFF_URLS = [
    ("manage:save_the_date", {}),
    ("manage:submissions", {}),
    ("manage:campaigns", {}),
    ("manage:campaign_new", {}),
    ("manage:email_templates", {}),
    ("manage:email_template", {"key": "save_the_date"}),
]


@pytest.fixture
def wedding(db):
    wedding = WeddingSettings.load()
    wedding.couple_names = "Sarah & Frank"
    wedding.wedding_city = "Chicago"
    wedding.contact_email = "hello@example.com"
    wedding.save()
    return WeddingSettings.load()


@pytest.fixture
def households(wedding):
    texted = Household.objects.create(
        name="The Texteds", phone="(312) 555-0101", save_the_date_texted_at=timezone.now()
    )
    phone_only = Household.objects.create(name="The Phones", phone="(312) 555-0102")
    emailable = Household.objects.create(name="The Emails", email="emails@example.com")
    confirmed = Household.objects.create(
        name="The Confirmeds", email="confirmed@example.com", address_confirmed_at=timezone.now()
    )
    nothing = Household.objects.create(name="The Nothings")
    for household, first in (
        (texted, "Tess"),
        (phone_only, "Phil"),
        (emailable, "Emma"),
        (confirmed, "Cora"),
        (nothing, "Ned"),
    ):
        Guest.objects.create(household=household, first_name=first, last_name=household.name.split()[1][:-1])
    return {"texted": texted, "phone": phone_only, "email": emailable, "confirmed": confirmed, "nothing": nothing}


def _jobs(class_name: str) -> int:
    return Job.objects.filter(class_name=class_name).count()


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "kwargs"), STAFF_URLS)
def test_staff_only(client, name, kwargs):
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "kwargs"), STAFF_URLS)
def test_staff_pages_render(admin_client, wedding, name, kwargs):
    assert admin_client.get(reverse(name, kwargs=kwargs)).status_code == 200


# --- Save the date ----------------------------------------------------------------------


@pytest.mark.django_db
class TestSaveTheDatePage:
    def test_stats_and_sms_link(self, admin_client, households):
        response = admin_client.get(reverse("manage:save_the_date"))
        stats = response.context["stats"]
        assert stats == {
            "total": 5,
            "confirmed": 1,
            "texted": 1,
            "emailed": 0,
            "unreached": 3,
            "to_text": 1,
            "can_email": 2,
            "no_way": 1,
        }
        row = next(row for row in response.context["rows"] if row["household"] == households["phone"])
        assert row["sms_href"].startswith("sms:+13125550102?&body=")
        assert households["phone"].save_the_date_url in unquote(row["sms_href"])
        nothing = next(row for row in response.context["rows"] if row["household"] == households["nothing"])
        assert nothing["unreachable"] and nothing["sms_href"] == ""

    def test_emailed_counts_save_the_date_campaigns(self, admin_client, households):
        campaign = Campaign.objects.create(name="STD", template_key="save_the_date", audience="all_with_email")
        CampaignRecipient.objects.create(
            campaign=campaign, household=households["email"], status=CampaignRecipient.Status.SENT
        )
        response = admin_client.get(reverse("manage:save_the_date"))
        assert response.context["stats"]["emailed"] == 1
        assert response.context["stats"]["unreached"] == 2

    @pytest.mark.parametrize(
        ("show", "expected"),
        [
            ("to_text", {"The Phones"}),
            ("unreached", {"The Phones", "The Emails", "The Nothings"}),
            ("no_address", {"The Texteds", "The Phones", "The Emails", "The Nothings"}),
        ],
    )
    def test_filters(self, admin_client, households, show, expected):
        response = admin_client.get(reverse("manage:save_the_date"), {"show": show})
        assert {row["household"].name for row in response.context["rows"]} == expected

    def test_search_returns_rows_partial_for_htmx(self, admin_client, households):
        response = admin_client.get(
            reverse("manage:save_the_date"), {"q": "phil"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="std-rows"
        )
        assert [row["household"].name for row in response.context["rows"]] == ["The Phones"]
        assert b"<html" not in response.content

    def test_mark_and_unmark_texted(self, admin_client, households):
        household = households["phone"]
        url = reverse("manage:household_texted", args=[household.pk])
        response = admin_client.post(url, {"texted": "1"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert b"Undo texted" in response.content
        household.refresh_from_db()
        assert household.save_the_date_texted_at is not None

        response = admin_client.post(url, {"texted": "0"})
        assert response.status_code == 302
        household.refresh_from_db()
        assert household.save_the_date_texted_at is None

    def test_switches(self, admin_client, wedding):
        url = reverse("manage:save_the_date_switch")
        admin_client.post(url, {"name": "codes_frozen", "on": "1"})
        admin_client.post(url, {"name": "save_the_date_open", "on": "1"})
        wedding = WeddingSettings.load()
        assert wedding.codes_frozen and wedding.save_the_date_open
        admin_client.post(url, {"name": "save_the_date_open", "on": "0"})
        assert not WeddingSettings.load().save_the_date_open

    def test_switch_rejects_other_settings(self, admin_client, wedding):
        admin_client.post(reverse("manage:save_the_date_switch"), {"name": "rsvp_open", "on": "1"})
        assert not WeddingSettings.load().rsvp_open


# --- Submissions ------------------------------------------------------------------------


@pytest.fixture
def submission(wedding):
    return ContactSubmission.objects.create(
        names="Bob & Carol Jones",
        email="bob@example.com",
        phone="(312) 555-0199",
        address_line1="1 Main St",
        city="Chicago",
        state="IL",
        postal_code="60601",
    )


@pytest.mark.django_db
class TestSubmissions:
    def test_suggestions_rank_contact_matches_first(self, submission):
        by_name = Household.objects.create(name="The Joneses")
        Guest.objects.create(household=by_name, first_name="Ann", last_name="Jones")
        by_email = Household.objects.create(name="Bob and Carol", email="BOB@example.com")
        Household.objects.create(name="Unrelated")
        assert suggest_households(submission) == [by_email, by_name]

    def test_merge(self, admin_client, submission):
        household = Household.objects.create(name="The Joneses")
        response = admin_client.post(
            reverse("manage:submission_merge", args=[submission.pk]), {"household": household.pk}
        )
        assert response.status_code == 302
        household.refresh_from_db()
        submission.refresh_from_db()
        assert submission.status == ContactSubmission.Status.MERGED
        assert household.address_line1 == "1 Main St"
        assert household.email == "bob@example.com"
        assert household.address_confirmed

    def test_merge_needs_a_household(self, admin_client, submission):
        admin_client.post(reverse("manage:submission_merge", args=[submission.pk]), {"household": ""})
        submission.refresh_from_db()
        assert submission.status == ContactSubmission.Status.NEW

    def test_create_household(self, admin_client, submission):
        admin_client.post(reverse("manage:submission_create", args=[submission.pk]))
        household = Household.objects.get(name="Bob & Carol Jones")
        assert [(g.first_name, g.last_name) for g in household.guests.order_by("sort_order")] == [
            ("Bob", "Jones"),
            ("Carol", "Jones"),
        ]
        assert household.address_confirmed
        submission.refresh_from_db()
        assert submission.household == household

    def test_create_refuses_a_taken_name(self, submission):
        Household.objects.create(name="bob & carol jones")
        with pytest.raises(SubmissionError):
            create_household(submission)
        assert Household.objects.count() == 1

    def test_handled_submission_cannot_be_merged_again(self, admin_client, submission):
        first = Household.objects.create(name="First")
        second = Household.objects.create(name="Second")
        url = reverse("manage:submission_merge", args=[submission.pk])
        admin_client.post(url, {"household": first.pk})
        admin_client.post(url, {"household": second.pk})
        second.refresh_from_db()
        assert second.email == ""

    def test_ignore_and_restore(self, admin_client, submission):
        url = reverse("manage:submission_status", args=[submission.pk])
        admin_client.post(url, {"status": "ignored"})
        submission.refresh_from_db()
        assert submission.status == ContactSubmission.Status.IGNORED
        admin_client.post(url, {"status": "new"})
        submission.refresh_from_db()
        assert submission.status == ContactSubmission.Status.NEW

    def test_list_filters_by_status(self, admin_client, submission):
        response = admin_client.get(reverse("manage:submissions"))
        assert [item["submission"] for item in response.context["items"]] == [submission]
        response = admin_client.get(reverse("manage:submissions"), {"status": "merged"})
        assert response.context["items"] == []


# --- Campaigns --------------------------------------------------------------------------


@pytest.fixture
def draft(households):
    return Campaign.objects.create(
        name="Save the date", template_key="save_the_date", audience=Campaign.Audience.ALL_WITH_EMAIL, daily_cap=1
    )


@pytest.mark.django_db
class TestCampaignPages:
    def test_new_prefills_from_query(self, admin_client, wedding):
        response = admin_client.get(
            reverse("manage:campaign_new"),
            {"template": "address_nudge", "audience": "missing_address", "name": "Nudge"},
        )
        initial = response.context["form"].initial
        assert initial["template_key"] == "address_nudge"
        assert initial["audience"] == "missing_address"
        assert initial["name"] == "Nudge"

    def test_create(self, admin_client, admin_user, households):
        response = admin_client.post(
            reverse("manage:campaign_new"),
            {"name": "STD", "template_key": "save_the_date", "audience": "all_with_email", "daily_cap": 25},
        )
        campaign = Campaign.objects.get()
        assert response["Location"] == reverse("manage:campaign_detail", args=[campaign.pk])
        assert campaign.created_by == admin_user
        assert campaign.status == Campaign.Status.DRAFT

    def test_selected_needs_households_and_attending_needs_event(self, admin_client, households):
        url = reverse("manage:campaign_new")
        base = {"name": "X", "template_key": "announcement", "daily_cap": 25}
        response = admin_client.post(url, {**base, "audience": "selected"})
        assert "households" in response.context["form"].errors
        response = admin_client.post(url, {**base, "audience": "attending"})
        assert "event" in response.context["form"].errors
        assert not Campaign.objects.exists()

    def test_selected_households_are_saved(self, admin_client, households):
        admin_client.post(
            reverse("manage:campaign_new"),
            {
                "name": "Two",
                "template_key": "save_the_date",
                "audience": "selected",
                "households": [households["email"].pk, households["nothing"].pk],
                "daily_cap": 25,
            },
        )
        campaign = Campaign.objects.get()
        response = admin_client.get(reverse("manage:campaign_detail", args=[campaign.pk]))
        assert [h.name for h in response.context["audience"]] == ["The Emails"]
        assert [h.name for h in response.context["skipped"]] == ["The Nothings"]

    def test_draft_detail_shows_audience_and_preview(self, admin_client, draft, households):
        response = admin_client.get(reverse("manage:campaign_detail", args=[draft.pk]))
        assert response.context["audience_size"] == 2
        assert response.context["days"] == 2
        assert response.context["preview_household"] == households["confirmed"]
        assert households["confirmed"].save_the_date_url in response.context["preview"]["html"]

        response = admin_client.get(
            reverse("manage:campaign_detail", args=[draft.pk]), {"household": households["email"].pk}
        )
        assert "Emma" in response.context["preview"]["html"]

    def test_send_needs_confirmation(self, admin_client, draft):
        admin_client.post(reverse("manage:campaign_send", args=[draft.pk]))
        draft.refresh_from_db()
        assert draft.status == Campaign.Status.DRAFT
        assert _jobs(SEND_CAMPAIGN_TASK) == 0

    def test_send(self, admin_client, draft):
        admin_client.post(reverse("manage:campaign_send", args=[draft.pk]), {"confirm": "1"})
        draft.refresh_from_db()
        assert draft.status == Campaign.Status.SENDING
        assert _jobs(SEND_CAMPAIGN_TASK) == 1

    def test_a_sending_campaign_cannot_be_edited_or_deleted(self, admin_client, draft):
        start_campaign(draft)
        response = admin_client.get(reverse("manage:campaign_edit", args=[draft.pk]))
        assert response.status_code == 302
        admin_client.post(reverse("manage:campaign_delete", args=[draft.pk]))
        assert Campaign.objects.filter(pk=draft.pk).exists()

    def test_delete_draft(self, admin_client, draft):
        admin_client.post(reverse("manage:campaign_delete", args=[draft.pk]))
        assert not Campaign.objects.exists()

    def test_next_batch_and_cap(self, admin_client, draft):
        admin_client.post(reverse("manage:campaign_next_batch", args=[draft.pk]))
        assert _jobs(SEND_CAMPAIGN_TASK) == 0  # still a draft
        start_campaign(draft)
        admin_client.post(reverse("manage:campaign_next_batch", args=[draft.pk]))
        assert _jobs(SEND_CAMPAIGN_TASK) == 2
        admin_client.post(reverse("manage:campaign_cap", args=[draft.pk]), {"daily_cap": "50"})
        draft.refresh_from_db()
        assert draft.daily_cap == 50

    def test_sending_detail_shows_progress(self, admin_client, draft, households):
        draft.status = Campaign.Status.SENDING
        draft.save()
        delivered = OutboundEmail.objects.create(
            to_email="confirmed@example.com", subject_rendered="s", status=OutboundEmail.DeliveryStatus.SENT
        )
        CampaignRecipient.objects.create(
            campaign=draft, household=households["confirmed"], status="sent", outbound_email=delivered
        )
        CampaignRecipient.objects.create(campaign=draft, household=households["email"], status="queued")
        CampaignRecipient.objects.create(campaign=draft, household=households["nothing"], status="skipped")
        response = admin_client.get(reverse("manage:campaign_detail", args=[draft.pk]))
        counts = response.context["counts"]
        assert counts["delivered"] == 1
        assert counts["queued"] == 1
        assert counts["skipped"] == 1
        assert response.context["days_left"] == 1

    def test_send_test_to_me(self, admin_client, admin_user, draft, households):
        admin_client.post(reverse("manage:campaign_test", args=[draft.pk]), {"household": households["email"].pk})
        email = OutboundEmail.objects.get()
        assert email.to_email == admin_user.email
        assert email.subject_rendered.startswith(TEST_SUBJECT_PREFIX)
        assert "Emma" in email.body_html_rendered
        assert not CampaignRecipient.objects.exists()


@pytest.mark.django_db
class TestAnnouncementAndRetry:
    def test_announcement_sends_the_campaign_message(self, households):
        campaign = Campaign.objects.create(
            name="Hotel", template_key="announcement", audience="all_with_email", message=""
        )
        with pytest.raises(Exception, match="message"):
            start_campaign(campaign)
        campaign.message = "<p>Rooms at the Drake.</p>"
        campaign.save()
        start_campaign(campaign)
        from guests.campaigns import send_campaign_batch

        send_campaign_batch(campaign)
        assert "Rooms at the Drake." in OutboundEmail.objects.first().body_html_rendered

    def test_retry_failed(self, draft, households):
        draft.status = Campaign.Status.SENT
        draft.save()
        refused = OutboundEmail.objects.create(
            to_email="emails@example.com", subject_rendered="s", status=OutboundEmail.DeliveryStatus.FAILED
        )
        CampaignRecipient.objects.create(
            campaign=draft, household=households["email"], status="sent", outbound_email=refused
        )
        CampaignRecipient.objects.create(
            campaign=draft, household=households["confirmed"], status="failed", error="boom"
        )
        assert retry_failed(draft) == {"recipients": 1, "emails": 1}
        draft.refresh_from_db()
        refused.refresh_from_db()
        assert draft.status == Campaign.Status.SENDING
        assert refused.status == OutboundEmail.DeliveryStatus.QUEUED
        assert draft.recipients.filter(status="queued").count() == 1
        assert _jobs(SEND_CAMPAIGN_TASK) == 1
        assert _jobs(SEND_EMAIL_TASK) == 1


# --- Email templates --------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailTemplates:
    def test_list_shows_every_registered_template(self, admin_client, wedding):
        response = admin_client.get(reverse("manage:email_templates"))
        keys = {row["key"] for row in response.context["rows"]}
        assert {"save_the_date", "address_nudge", "rsvp_confirmation"} <= keys

    def test_save(self, admin_client, wedding):
        url = reverse("manage:email_template", args=["save_the_date"])
        response = admin_client.post(
            url, {"subject_template": "Save the date, {{ contact_names }}", "html_body_template": "<p>Hi</p>"}
        )
        assert response.status_code == 302
        stored = EmailTemplateVersion.objects.get(template_key="save_the_date")
        assert stored.subject_template == "Save the date, {{ contact_names }}"

    def test_broken_template_is_not_saved(self, admin_client, wedding):
        before = EmailTemplateVersion.objects.get(template_key="save_the_date").html_body_template
        response = admin_client.post(
            reverse("manage:email_template", args=["save_the_date"]),
            {"subject_template": "x", "html_body_template": "{% if %}"},
        )
        assert response.status_code == 200
        assert "html_body_template" in response.context["form"].errors
        assert EmailTemplateVersion.objects.get(template_key="save_the_date").html_body_template == before

    def test_live_preview_uses_a_real_household(self, admin_client, households):
        response = admin_client.post(
            reverse("manage:email_template_preview", args=["save_the_date"]),
            {
                "subject_template": "For {{ contact_names }}",
                "html_body_template": "<a href='{{ save_the_date_url }}'>go</a>",
                "household": households["email"].pk,
            },
        )
        assert response.context["preview"]["subject"] == "For Emma"
        assert households["email"].save_the_date_url in response.context["preview"]["html"]

    def test_automatic_template_previews_with_sample_data(self, admin_client, wedding):
        response = admin_client.get(reverse("manage:email_template", args=["rsvp_confirmation"]))
        assert response.context["preview"]["error"] == ""
        assert response.context["households"] == []

    def test_unknown_template_404s(self, admin_client, wedding):
        assert admin_client.get(reverse("manage:email_template", args=["nope"])).status_code == 404

    def test_send_test(self, admin_client, admin_user, wedding):
        admin_client.post(reverse("manage:email_template_test", args=["rsvp_confirmation"]))
        email = OutboundEmail.objects.get()
        assert email.to_email == admin_user.email
        assert email.subject_rendered.startswith(TEST_SUBJECT_PREFIX)


@pytest.mark.django_db
def test_event_is_offered_for_attending_audience(admin_client, wedding):
    Event.objects.create(name="Reception", slug="reception", starts_at=timezone.now())
    response = admin_client.get(reverse("manage:campaign_new"))
    assert [event.name for event in response.context["form"].fields["event"].queryset] == ["Reception"]


@pytest.mark.django_db
def test_ampersands_are_plain_in_subject_and_text_but_escaped_in_html(households):
    """Couple names like "Sarah & Frank" must not reach an inbox as "Sarah &amp; Frank"."""
    from guests.email_contexts import build_save_the_date_context
    from messaging.services import EmailTemplateService

    template = EmailTemplateVersion.objects.get(template_key="save_the_date")
    template.subject_template = "Save the date: {{ couple_names }}\n"
    template.html_body_template = "<p>{{ couple_names }}</p>"
    template.save()
    email = EmailTemplateService().queue_email(
        template_key="save_the_date",
        to_email="x@example.com",
        context_data=build_save_the_date_context(households["email"], WeddingSettings.load()),
    )
    assert email.subject_rendered == "Save the date: Sarah & Frank"
    assert email.body_text_rendered == "Sarah & Frank"
    assert email.body_html_rendered == "<p>Sarah &amp; Frank</p>"
