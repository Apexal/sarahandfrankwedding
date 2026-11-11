"""Render a wedding email for a real household, and send test copies, for the /manage/ email pages.

A campaign template is rendered with the context a real send would use (`CAMPAIGN_CONTEXT_BUILDERS`),
so the preview shows a household's actual names and link. Templates the RSVP flow sends
(confirmation, internal notice) have no household builder and render with their preview defaults.
"""

from dataclasses import asdict, fields, is_dataclass, replace

import structlog
from django.template import Context, Template, TemplateSyntaxError

from guests.campaigns import CAMPAIGN_CONTEXT_BUILDERS
from guests.email_contexts import ANNOUNCEMENT
from guests.models import Campaign, Household
from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.services import EmailTemplateService, html_to_text, render_subject
from messaging.template_definitions import get_context_default_values, get_template_definition
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

TEST_SUBJECT_PREFIX = "[Test] "


class PreviewError(Exception):
    """The email cannot be rendered or sent as it stands; the message is fit to show us."""


def context_object(template_key: str, household: Household | None, *, campaign: Campaign | None = None) -> object:
    """The context dataclass instance a send would use: built for `household` when the template has a
    builder and a household is given, else filled from the template's preview defaults."""
    definition = get_template_definition(template_key)
    if definition is None:
        raise PreviewError(f"'{template_key}' is not a registered email template.")
    builder = CAMPAIGN_CONTEXT_BUILDERS.get(template_key)
    defaults = get_context_default_values(template_key)
    if builder is not None and household is not None:
        context = builder(household, WeddingSettings.load(), campaign)
        if template_key == ANNOUNCEMENT and campaign is None:
            # Editing the template itself: there is no campaign message yet, so show the sample one.
            context = replace(context, body_html=defaults.get("body_html", ""))
        return context
    return definition.context_dataclass(
        **{field.name: defaults.get(field.name, "") for field in fields(definition.context_dataclass)}
    )


def _render(source: str, context: dict) -> str:
    if not source:
        return ""
    try:
        return Template(source).render(Context(context))
    except TemplateSyntaxError as exc:
        raise PreviewError(f"Template error: {exc}") from exc


def render(template_key: str, subject_source: str, body_source: str, context: object) -> dict:
    """`{"subject", "html", "error"}` for the given sources; `error` is set instead of raising."""
    data = asdict(context) if is_dataclass(context) else dict(context)
    try:
        subject = render_subject(subject_source, data)
        html = _render(body_source, data)
    except TemplateSyntaxError as exc:
        return {"subject": "", "html": "", "error": f"Template error: {exc}"}
    except PreviewError as exc:
        return {"subject": "", "html": "", "error": str(exc)}
    if not subject:
        definition = get_template_definition(template_key)
        subject = definition.name if definition else template_key
    return {"subject": subject, "html": html, "text": html_to_text(html), "error": ""}


def stored_template(template_key: str) -> EmailTemplateVersion | None:
    return EmailTemplateVersion.objects.filter(template_key=template_key).first()


def render_stored(template_key: str, household: Household | None, *, campaign: Campaign | None = None) -> dict:
    """Render the saved template for a household (or the sample data)."""
    template = stored_template(template_key)
    if template is None:
        return {"subject": "", "html": "", "error": f"There is no saved “{template_key}” template yet."}
    context = context_object(template_key, household, campaign=campaign)
    return render(template_key, template.subject_template, template.html_body_template, context)


def send_test(
    template_key: str, to_email: str, household: Household | None, *, campaign: Campaign | None = None
) -> OutboundEmail:
    """Queue the saved template, rendered for `household`, to `to_email` with "[Test]" on the subject."""
    if not to_email:
        raise PreviewError("Your account has no email address to send the test to.")
    preview = render_stored(template_key, household, campaign=campaign)
    if preview["error"]:
        raise PreviewError(preview["error"])
    outbound_email = EmailTemplateService().queue_email(
        template_key=template_key,
        to_email=to_email,
        context_data=context_object(template_key, household, campaign=campaign),
        subject=f"{TEST_SUBJECT_PREFIX}{preview['subject']}",
        reply_to=WeddingSettings.load().contact_email or None,
    )
    logger.info(
        "test_email_sent",
        template_key=template_key,
        household_id=household.pk if household else None,
        campaign_id=campaign.pk if campaign else None,
        outbound_email_id=outbound_email.pk,
    )
    return outbound_email
