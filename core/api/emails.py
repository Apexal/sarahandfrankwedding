"""Transactional email for the mobile API.

Routes through the same `OutboundEmail` audit row + rate-limited "email" queue as
`CoreAccountAdapter.send_mail`, but renders from file-based Django templates
rather than `messaging`'s DB-stored templates — a fresh clone has no
`EmailTemplateVersion` rows, and registration must work before anyone opens the
admin. Move a template into `messaging` once non-developers need to edit its copy.
"""

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from messaging.models import OutboundEmail
from messaging.tasks import send_outbound_email_task


def send_templated_email(*, template_key: str, to_email: str, subject: str, context: dict) -> OutboundEmail:
    """Render `core/email/<template_key>.html` and enqueue it for delivery."""
    html_body = render_to_string(f"core/email/{template_key}.html", context)
    outbound_email = OutboundEmail.objects.create(
        to_email=to_email,
        from_email=settings.DEFAULT_FROM_EMAIL or "",
        subject_rendered=subject,
        body_html_rendered=html_body,
        body_text_rendered=strip_tags(html_body),
        template_key=template_key,
    )
    send_outbound_email_task.enqueue(outbound_email_id=outbound_email.pk, fail_silently=False)
    return outbound_email
