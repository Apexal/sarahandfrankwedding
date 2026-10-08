from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from messaging import template_definitions
from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.services import (
    EmailTemplateContextTypeError,
    EmailTemplateNotFoundError,
    EmailTemplateRenderError,
    EmailTemplateService,
)
from messaging.tasks import _prune_outbound_emails, _send_outbound_email
from messaging.template_definitions import TemplateDefinition


@dataclass
class WelcomeContext:
    first_name: str


@dataclass
class WrongContext:
    # Same shape as WelcomeContext — the contract is the declared type, not the fields.
    first_name: str


@pytest.mark.django_db
class TestEmailTemplateService:
    """send_email enforces the context-dataclass contract declared in template_definitions."""

    @pytest.fixture(autouse=True)
    def _welcome_definition(self, monkeypatch):
        # The starter ships with no definitions; register a test-only one. Patch
        # the mapping (read at call time), not the imported lookup functions.
        monkeypatch.setattr(
            template_definitions,
            "_TEMPLATE_DEFINITIONS",
            MappingProxyType(
                {"welcome": TemplateDefinition(key="welcome", name="Welcome", context_dataclass=WelcomeContext)}
            ),
        )

    @pytest.fixture
    def welcome_template(self, _welcome_definition):
        return EmailTemplateVersion.objects.create(
            template_key="welcome",
            subject_template="Hi {{ first_name }}",
            html_body_template="<p>Hello {{ first_name }}!</p>",
        )

    def _send(self, **overrides):
        kwargs = {
            "template_key": "welcome",
            "to_email": "ada@example.com",
            "context_data": WelcomeContext(first_name="Ada"),
        }
        kwargs.update(overrides)
        return EmailTemplateService().send_email(**kwargs)

    def test_renders_persists_and_enqueues(self, welcome_template):
        self._send()

        outbound_email = OutboundEmail.objects.get(to_email="ada@example.com")
        assert outbound_email.subject_rendered == "Hi Ada"
        assert outbound_email.body_html_rendered == "<p>Hello Ada!</p>"
        assert outbound_email.body_text_rendered == "Hello Ada!"
        assert outbound_email.send_context == {"first_name": "Ada"}
        assert outbound_email.template_version == welcome_template
        assert outbound_email.status == OutboundEmail.DeliveryStatus.QUEUED

    def test_rejects_wrong_context_dataclass(self, welcome_template):
        with pytest.raises(EmailTemplateContextTypeError, match="WelcomeContext"):
            self._send(context_data=WrongContext(first_name="Ada"))
        assert not OutboundEmail.objects.exists()

    def test_rejects_non_dataclass_context(self, welcome_template):
        with pytest.raises(EmailTemplateContextTypeError):
            self._send(context_data={"first_name": "Ada"})
        assert not OutboundEmail.objects.exists()

    def test_missing_template_row(self):
        with pytest.raises(EmailTemplateNotFoundError, match="welcome"):
            self._send()

    def test_template_syntax_error(self):
        EmailTemplateVersion.objects.create(template_key="welcome", html_body_template="{% bad %}")
        with pytest.raises(EmailTemplateRenderError):
            self._send()
        assert not OutboundEmail.objects.exists()


@pytest.mark.django_db
class TestPruneOutboundEmails:
    def test_deletes_only_rows_past_retention(self):
        old_email = OutboundEmail.objects.create(to_email="old@example.com", subject_rendered="old")
        recent_email = OutboundEmail.objects.create(to_email="new@example.com", subject_rendered="new")
        OutboundEmail.objects.filter(pk=old_email.pk).update(created_at=timezone.now() - timedelta(days=120))

        deleted_count = _prune_outbound_emails()

        assert deleted_count == 1
        assert not OutboundEmail.objects.filter(pk=old_email.pk).exists()
        assert OutboundEmail.objects.filter(pk=recent_email.pk).exists()


@pytest.mark.django_db
class TestRequeueAdminAction:
    def test_requeues_failed_but_not_sent(self, admin_client):
        failed_email = OutboundEmail.objects.create(
            to_email="failed@example.com",
            subject_rendered="failed",
            status=OutboundEmail.DeliveryStatus.FAILED,
            error_message="boom",
        )
        sent_email = OutboundEmail.objects.create(
            to_email="sent@example.com",
            subject_rendered="sent",
            status=OutboundEmail.DeliveryStatus.SENT,
        )
        admin_client.post(
            reverse("admin:messaging_outboundemail_changelist"),
            {"action": "requeue_emails", "_selected_action": [failed_email.pk, sent_email.pk]},
        )

        failed_email.refresh_from_db()
        sent_email.refresh_from_db()
        assert failed_email.status == OutboundEmail.DeliveryStatus.QUEUED
        assert failed_email.error_message == ""
        assert sent_email.status == OutboundEmail.DeliveryStatus.SENT


@pytest.mark.django_db
class TestAllauthEmailPipeline:
    def test_password_reset_creates_outbound_email(self, client, user):
        response = client.post("/accounts/password/reset/", {"email": user.email})

        assert response.status_code == 302
        outbound_email = OutboundEmail.objects.get(to_email=user.email)
        assert outbound_email.template_key == "password_reset_key"
        assert outbound_email.status == OutboundEmail.DeliveryStatus.QUEUED
        assert outbound_email.subject_rendered
        assert outbound_email.body_text_rendered


class TestRegisterTemplateDefinition:
    @pytest.fixture(autouse=True)
    def _fresh_registry(self, monkeypatch):
        monkeypatch.setattr(template_definitions, "_TEMPLATE_DEFINITIONS", {})

    def test_registers_and_is_idempotent(self):
        definition = TemplateDefinition(key="welcome", name="Welcome", context_dataclass=WelcomeContext)
        template_definitions.register_template_definition(definition)
        template_definitions.register_template_definition(definition)
        assert template_definitions.get_allowed_template_keys() == ["welcome"]
        assert template_definitions.get_template_definition("welcome") is definition
        assert template_definitions.get_context_variable_names("welcome") == ["first_name"]

    def test_rejects_a_different_dataclass_for_the_same_key(self):
        template_definitions.register_template_definition(
            TemplateDefinition(key="welcome", name="Welcome", context_dataclass=WelcomeContext)
        )
        with pytest.raises(ValueError, match="already registered"):
            template_definitions.register_template_definition(
                TemplateDefinition(key="welcome", name="Welcome", context_dataclass=WrongContext)
            )
        assert template_definitions.get_template_definition("welcome").context_dataclass is WelcomeContext


@pytest.mark.django_db
class TestRecipientAllowlist:
    """With EMAIL_RECIPIENT_ALLOWLIST set, staging can never email a real guest."""

    def _outbound(self, to_email="guest@example.com"):
        return OutboundEmail.objects.create(
            to_email=to_email,
            subject_rendered="Save the date",
            body_text_rendered="hi",
            body_html_rendered="<p>hi</p>",
            template_key="save_the_date",
        )

    def test_redirects_outside_addresses_and_keeps_the_original_on_the_row(self, settings):
        settings.EMAIL_RECIPIENT_ALLOWLIST = ["frank@example.com", "sarah@example.com"]
        outbound_email = self._outbound()

        _send_outbound_email(outbound_email_id=outbound_email.pk)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["frank@example.com"]
        assert mail.outbox[0].subject == "[to: guest@example.com] Save the date"
        outbound_email.refresh_from_db()
        assert outbound_email.to_email == "guest@example.com"
        assert outbound_email.status == OutboundEmail.DeliveryStatus.SENT

    def test_allowed_addresses_are_delivered_as_is(self, settings):
        settings.EMAIL_RECIPIENT_ALLOWLIST = ["frank@example.com"]
        outbound_email = self._outbound(to_email="Frank@Example.com")

        _send_outbound_email(outbound_email_id=outbound_email.pk)

        assert mail.outbox[0].to == ["Frank@Example.com"]
        assert mail.outbox[0].subject == "Save the date"

    def test_empty_allowlist_changes_nothing(self, settings):
        settings.EMAIL_RECIPIENT_ALLOWLIST = []
        outbound_email = self._outbound()

        _send_outbound_email(outbound_email_id=outbound_email.pk)

        assert mail.outbox[0].to == ["guest@example.com"]
        assert mail.outbox[0].subject == "Save the date"
