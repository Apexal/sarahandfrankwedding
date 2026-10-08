"""Email slice: template registration and seeding, campaign sending, resume task, bounces."""

import importlib

import pytest
from anymail.signals import AnymailTrackingEvent, tracking
from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone
from steady_queue.arguments import Arguments
from steady_queue.models import Job

from guests import email_contexts
from guests.campaigns import CampaignError, send_campaign_batch, start_campaign
from guests.email_contexts import (
    TEMPLATE_KEYS,
    build_announcement_context,
    build_rsvp_nudge_context,
    build_save_the_date_context,
    rsvp_deadline_display,
    wedding_date_display,
)
from guests.models import Campaign, CampaignRecipient, Guest, Household
from guests.tasks import _resume_campaigns
from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.services import EmailTemplateRenderError, EmailTemplateService
from messaging.template_definitions import (
    get_allowed_template_keys,
    get_context_default_values,
    get_context_variable_names,
    register_template_definition,
)
from pages.models import WeddingSettings

SEND_CAMPAIGN_TASK = "guests.tasks.send_campaign"
SEND_EMAIL_TASK = "messaging.tasks.send_outbound_email_task"


def enqueued_kwargs(class_name: str) -> list[dict]:
    """Keyword arguments of every queued job for a task, oldest first."""
    return [
        Arguments.deserialize_args_and_kwargs(job.arguments["arguments"])[1]
        for job in Job.objects.filter(class_name=class_name).order_by("pk")
    ]


@pytest.fixture
def wedding(db):
    wedding = WeddingSettings.load()
    wedding.couple_names = "Sarah & Frank"
    wedding.wedding_city = "Chicago, Illinois"
    wedding.contact_email = "hello@example.com"
    wedding.save()
    return WeddingSettings.load()


@pytest.fixture
def households(wedding):
    made = []
    for index, name in enumerate(("Adams", "Baker", "Clark")):
        household = Household.objects.create(name=f"The {name}s", email=f"{name.lower()}@example.com")
        Guest.objects.create(household=household, first_name=f"{name}One", last_name=name)
        Guest.objects.create(household=household, first_name=f"{name}Two", last_name=name, sort_order=1)
        made.append(household)
        del index
    return made


@pytest.fixture
def campaign(households):
    return Campaign.objects.create(
        name="Save the date",
        template_key="save_the_date",
        audience=Campaign.Audience.ALL_WITH_EMAIL,
        status=Campaign.Status.SENDING,
        daily_cap=2,
    )


# --- Registration and context builders -------------------------------------------------


class TestTemplateRegistration:
    def test_six_keys_are_registered_with_the_shared_footer_fields(self):
        assert set(TEMPLATE_KEYS) <= set(get_allowed_template_keys())
        for key in TEMPLATE_KEYS:
            names = get_context_variable_names(key)
            assert {"couple_names", "contact_names", "save_the_date_url", "site_url", "contact_email"} <= set(names)
            defaults = get_context_default_values(key)
            assert set(defaults) == set(names), key

    def test_re_registration_is_idempotent(self):
        # ready() imports the module once, but registering again (the same objects) must be harmless.
        for definition in email_contexts.TEMPLATE_DEFINITIONS:
            register_template_definition(definition)
        assert set(TEMPLATE_KEYS) <= set(get_allowed_template_keys())

    def test_url_helpers_follow_app_base_url(self, settings):
        settings.APP_BASE_URL = "https://example.wedding/"
        assert email_contexts.site_url() == "https://example.wedding"
        assert email_contexts.ics_url() == "https://example.wedding/save-the-date/event.ics"
        assert email_contexts.rsvp_url() == "https://example.wedding/rsvp/"


@pytest.mark.django_db
class TestContextBuilders:
    def test_date_displays(self, wedding):
        assert wedding_date_display(wedding) == "September 18, 2027"
        assert rsvp_deadline_display(wedding) == ""
        wedding.rsvp_deadline = timezone.datetime(2027, 8, 1).date()
        assert rsvp_deadline_display(wedding) == "August 1, 2027"

    def test_save_the_date_context(self, wedding, households):
        household = households[0]
        context = build_save_the_date_context(household, wedding)
        assert context.couple_names == "Sarah & Frank"
        assert context.contact_names == "AdamsOne & AdamsTwo"
        assert context.household_name == "The Adamss"
        assert context.wedding_city == "Chicago, Illinois"
        assert context.save_the_date_url == household.save_the_date_url
        assert context.ics_url.endswith("/save-the-date/event.ics")
        assert context.contact_email == "hello@example.com"

    def test_rsvp_nudge_and_announcement_contexts(self, wedding, households):
        wedding.site_password = "september"
        household = households[0]
        nudge = build_rsvp_nudge_context(household, wedding)
        assert nudge.invite_code == household.invite_code
        assert nudge.site_password == "september"
        assert nudge.rsvp_url.endswith("/rsvp/")
        announcement = build_announcement_context(household, wedding, body_html="<p>Hi</p>")
        assert announcement.body_html == "<p>Hi</p>"


# --- Seeded template rows --------------------------------------------------------------


@pytest.mark.django_db
class TestSeededTemplates:
    def test_rows_exist_and_render_with_preview_defaults(self):
        for key in TEMPLATE_KEYS:
            row = EmailTemplateVersion.objects.get(template_key=key)
            defaults = get_context_default_values(key)
            html = Template(row.html_body_template).render(Context(defaults))
            subject = Template(row.subject_template).render(Context(defaults)).strip()
            assert defaults["save_the_date_url"] in html, key
            assert defaults["contact_email"] in html, key
            assert subject, key
            assert "djlint" not in row.html_body_template, key

    def test_reseeding_keeps_existing_rows(self):
        from django.apps import apps

        migration = importlib.import_module("guests.migrations.0002_seed_email_templates")
        row = EmailTemplateVersion.objects.get(template_key="save_the_date")
        row.subject_template = "Edited by hand"
        row.save()

        assert migration.seed_email_templates(apps, None) == 0

        row.refresh_from_db()
        assert row.subject_template == "Edited by hand"
        assert EmailTemplateVersion.objects.filter(template_key__in=TEMPLATE_KEYS).count() == len(TEMPLATE_KEYS)


# --- Starting and sending --------------------------------------------------------------


@pytest.mark.django_db
class TestStartCampaign:
    def test_refuses_without_a_template_or_when_not_a_draft(self, households):
        no_template = Campaign.objects.create(name="x", audience=Campaign.Audience.ALL_WITH_EMAIL)
        with pytest.raises(CampaignError, match="template"):
            start_campaign(no_template)
        already_sent = Campaign.objects.create(
            name="y", template_key="save_the_date", audience=Campaign.Audience.ALL_WITH_EMAIL, status="sent"
        )
        with pytest.raises(CampaignError, match="not a draft"):
            start_campaign(already_sent)
        not_a_campaign_template = Campaign.objects.create(
            name="z", template_key="rsvp_confirmation", audience=Campaign.Audience.ALL_WITH_EMAIL
        )
        with pytest.raises(CampaignError, match="rsvp_confirmation"):
            start_campaign(not_a_campaign_template)
        assert not Job.objects.filter(class_name=SEND_CAMPAIGN_TASK).exists()

    def test_marks_sending_and_enqueues_the_first_batch(self, households):
        campaign = Campaign.objects.create(
            name="x", template_key="save_the_date", audience=Campaign.Audience.ALL_WITH_EMAIL
        )
        start_campaign(campaign)
        campaign.refresh_from_db()
        assert campaign.status == Campaign.Status.SENDING
        assert enqueued_kwargs(SEND_CAMPAIGN_TASK) == [{"campaign_id": campaign.pk}]


@pytest.mark.django_db
class TestSendCampaignBatch:
    def test_creates_recipients_and_links_outbound_emails(self, campaign, households, wedding):
        handed_off = send_campaign_batch(campaign)

        assert handed_off == 2
        recipients = {r.household.name: r for r in campaign.recipients.select_related("household", "outbound_email")}
        assert set(recipients) == {"The Adamss", "The Bakers", "The Clarks"}
        assert recipients["The Clarks"].status == CampaignRecipient.Status.QUEUED
        for name in ("The Adamss", "The Bakers"):
            recipient = recipients[name]
            assert recipient.status == CampaignRecipient.Status.SENT
            outbound_email = recipient.outbound_email
            assert outbound_email is not None
            assert outbound_email.to_email == recipient.household.email
            assert outbound_email.template_key == "save_the_date"
            assert outbound_email.reply_to_email == "hello@example.com"
            assert outbound_email.status == OutboundEmail.DeliveryStatus.QUEUED
            assert recipient.household.save_the_date_url in outbound_email.body_html_rendered
            assert "September 18, 2027" in outbound_email.subject_rendered
        assert Job.objects.filter(class_name=SEND_EMAIL_TASK).count() == 2
        campaign.refresh_from_db()
        assert campaign.status == Campaign.Status.SENDING
        assert campaign.sent_at is None

    def test_daily_cap_across_runs_then_sent_then_idempotent(self, campaign, households):
        assert send_campaign_batch(campaign) == 2
        assert send_campaign_batch(campaign) == 1
        campaign.refresh_from_db()
        assert campaign.status == Campaign.Status.SENT
        assert campaign.sent_at is not None
        assert campaign.recipients.filter(status=CampaignRecipient.Status.SENT).count() == 3

        # Done campaigns stay done: nothing new is queued, counts do not move.
        assert send_campaign_batch(campaign) == 0
        assert campaign.recipients.count() == 3
        assert OutboundEmail.objects.count() == 3

    def test_selected_audience_records_skips_with_reasons(self, households, wedding):
        bounced = Household.objects.create(name="Bounced", email="b@example.com", email_bounced_at=timezone.now())
        opted_out = Household.objects.create(name="Out", email="o@example.com", email_opt_out=True)
        no_email = Household.objects.create(name="NoEmail")
        campaign = Campaign.objects.create(
            name="sel",
            template_key="address_nudge",
            audience=Campaign.Audience.SELECTED,
            status=Campaign.Status.SENDING,
            daily_cap=10,
        )
        campaign.households.set([households[0], bounced, opted_out, no_email])

        assert send_campaign_batch(campaign) == 1

        by_name = {r.household.name: r for r in campaign.recipients.select_related("household")}
        assert by_name["The Adamss"].status == CampaignRecipient.Status.SENT
        assert by_name["Bounced"].status == CampaignRecipient.Status.SKIPPED
        assert by_name["Bounced"].error.startswith("Email bounced on ")
        assert by_name["Out"].error == "Opted out of email."
        assert by_name["NoEmail"].error == "No email address."
        campaign.refresh_from_db()
        assert campaign.status == Campaign.Status.SENT

    def test_rendering_failure_marks_only_that_recipient_failed(self, campaign, households, monkeypatch):
        campaign.daily_cap = 10
        campaign.save()
        original = EmailTemplateService.queue_email

        def flaky(self, *, to_email, **kwargs):
            if to_email == "baker@example.com":
                raise EmailTemplateRenderError("boom")
            return original(self, to_email=to_email, **kwargs)

        monkeypatch.setattr(EmailTemplateService, "queue_email", flaky)

        assert send_campaign_batch(campaign) == 2

        failed = campaign.recipients.get(household__email="baker@example.com")
        assert failed.status == CampaignRecipient.Status.FAILED
        assert failed.error == "boom"
        assert failed.outbound_email is None
        assert campaign.recipients.filter(status=CampaignRecipient.Status.SENT).count() == 2
        campaign.refresh_from_db()
        assert campaign.status == Campaign.Status.SENT

    def test_does_nothing_for_a_draft(self, households):
        draft = Campaign.objects.create(name="d", template_key="save_the_date", audience="all_with_email")
        assert send_campaign_batch(draft) == 0
        assert not draft.recipients.exists()


@pytest.mark.django_db
class TestResumeCampaigns:
    def test_enqueues_only_sending_campaigns(self, households):
        Campaign.objects.create(name="draft", template_key="save_the_date", audience="all_with_email")
        sending = Campaign.objects.create(
            name="sending", template_key="save_the_date", audience="all_with_email", status="sending"
        )
        Campaign.objects.create(name="sent", template_key="save_the_date", audience="all_with_email", status="sent")

        assert _resume_campaigns() == 1
        assert enqueued_kwargs(SEND_CAMPAIGN_TASK) == [{"campaign_id": sending.pk}]


@pytest.mark.django_db
class TestCampaignAdminSendAction:
    def test_starts_drafts_with_a_template_and_reports_the_rest(self, admin_client, households):
        ready = Campaign.objects.create(name="ready", template_key="save_the_date", audience="all_with_email")
        no_template = Campaign.objects.create(name="no template", audience="all_with_email")

        response = admin_client.post(
            reverse("admin:guests_campaign_changelist"),
            {"action": "send_campaigns", "_selected_action": [ready.pk, no_template.pk]},
            follow=True,
        )

        ready.refresh_from_db()
        no_template.refresh_from_db()
        assert ready.status == Campaign.Status.SENDING
        assert no_template.status == Campaign.Status.DRAFT
        assert enqueued_kwargs(SEND_CAMPAIGN_TASK) == [{"campaign_id": ready.pk}]
        messages = [str(message) for message in response.context["messages"]]
        assert any("no template: Pick a template" in message for message in messages)
        assert any("Started 1 campaign(s)" in message for message in messages)

    def test_change_form_explains_sending(self, admin_client, households):
        campaign = Campaign.objects.create(name="c", template_key="save_the_date", audience="all_with_email")
        response = admin_client.get(reverse("admin:guests_campaign_change", args=[campaign.pk]))
        assert response.status_code == 200
        assert b"How sending works" in response.content


# --- Bounces ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBounceReceiver:
    def _fire(self, event_type, recipient):
        event = AnymailTrackingEvent(event_type=event_type, recipient=recipient, reject_reason="bounced")
        tracking.send(sender=object, event=event, esp_name="resend")

    def test_bounce_flags_every_matching_household_case_insensitively(self, households):
        twin = Household.objects.create(name="Adams twin", email="Adams@Example.com")

        self._fire("bounced", "ADAMS@example.com")

        for household in (households[0], twin):
            household.refresh_from_db()
            assert household.email_bounced_at is not None
        households[1].refresh_from_db()
        assert households[1].email_bounced_at is None

    def test_complaint_counts_but_delivery_does_not(self, households):
        self._fire("delivered", "adams@example.com")
        households[0].refresh_from_db()
        assert households[0].email_bounced_at is None

        self._fire("complained", "adams@example.com")
        households[0].refresh_from_db()
        assert households[0].email_bounced_at is not None

    def test_existing_timestamp_is_kept(self, households):
        first = timezone.now() - timezone.timedelta(days=3)
        Household.objects.filter(pk=households[0].pk).update(email_bounced_at=first)

        self._fire("bounced", "adams@example.com")

        households[0].refresh_from_db()
        assert households[0].email_bounced_at == first
