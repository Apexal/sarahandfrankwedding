from dataclasses import asdict, is_dataclass

from django.conf import settings
from django.tasks import TaskResult
from django.template import Context, Template, TemplateSyntaxError
from django.utils.html import strip_tags

from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.tasks import send_outbound_email_task
from messaging.template_definitions import (
    get_template_definition,
)


class EmailTemplateServiceError(Exception):
    """Base class for email-template send service exceptions."""


class EmailTemplateNotFoundError(EmailTemplateServiceError):
    """Raised when a template key cannot be resolved to stored HTML."""


class EmailTemplateRenderError(EmailTemplateServiceError):
    """Raised when HTML template rendering fails."""


class EmailTemplateContextTypeError(EmailTemplateServiceError):
    """Raised when provided context type does not match template requirements."""


class EmailTemplateService:
    """Sends emails by template key using stored HTML templates."""

    def send_email(
        self,
        *,
        template_key: str,
        to_email: str,
        context_data: object,
        subject: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
        fail_silently: bool = False,
    ) -> TaskResult:
        _, task_result = self._render_persist_and_enqueue(
            template_key=template_key,
            to_email=to_email,
            context_data=context_data,
            subject=subject,
            from_email=from_email,
            reply_to=reply_to,
            fail_silently=fail_silently,
        )
        return task_result

    def queue_email(
        self,
        *,
        template_key: str,
        to_email: str,
        context_data: object,
        subject: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
        fail_silently: bool = False,
    ) -> OutboundEmail:
        """Like `send_email`, but returns the `OutboundEmail` audit row instead of the task result.

        For callers that keep their own record of the send (a campaign recipient, say) and
        want to point at the row whose status the email worker will update.
        """
        outbound_email, _ = self._render_persist_and_enqueue(
            template_key=template_key,
            to_email=to_email,
            context_data=context_data,
            subject=subject,
            from_email=from_email,
            reply_to=reply_to,
            fail_silently=fail_silently,
        )
        return outbound_email

    def _render_persist_and_enqueue(
        self,
        *,
        template_key: str,
        to_email: str,
        context_data: object,
        subject: str | None,
        from_email: str | None,
        reply_to: str | None,
        fail_silently: bool,
    ) -> tuple[OutboundEmail, TaskResult]:
        template_html = self._get_template_html(template_key=template_key)
        normalized_context = self._normalize_context_data(
            template_key=template_key,
            context_data=context_data,
        )

        rendered_subject_template = self._render(
            template_html.subject_template,
            normalized_context,
        ).strip()
        html_body = self._render(template_html.html_body_template, normalized_context)
        text_body = strip_tags(html_body) if html_body else ""
        resolved_subject = subject or rendered_subject_template or self._default_subject_for_template_key(template_key)
        resolved_from_email = from_email or settings.DEFAULT_FROM_EMAIL

        outbound_email = OutboundEmail.objects.create(
            to_email=to_email,
            from_email=resolved_from_email or "",
            reply_to_email=reply_to or "",
            subject_rendered=resolved_subject,
            body_html_rendered=html_body,
            body_text_rendered=text_body,
            send_context=normalized_context,
            template_key=template_key,
            template_version=template_html,
        )

        task_result = send_outbound_email_task.enqueue(
            outbound_email_id=outbound_email.id,
            fail_silently=fail_silently,
        )
        return outbound_email, task_result

    def _get_template_html(self, *, template_key: str) -> EmailTemplateVersion:
        template_html = EmailTemplateVersion.objects.filter(template_key=template_key).first()
        if template_html is None:
            raise EmailTemplateNotFoundError(f"Email template HTML '{template_key}' does not exist.")
        return template_html

    def _default_subject_for_template_key(self, template_key: str) -> str:
        definition = get_template_definition(template_key)
        return definition.name if definition else template_key

    def _render(self, template_source: str, context_data: dict) -> str:
        if not template_source:
            return ""
        try:
            template = Template(template_source)
            return template.render(Context(context_data))
        except TemplateSyntaxError as exc:
            raise EmailTemplateRenderError(str(exc)) from exc

    def _normalize_context_data(
        self,
        *,
        template_key: str,
        context_data: object,
    ) -> dict[str, object]:
        template_definition = get_template_definition(template_key)
        expected_context_type = template_definition.context_dataclass if template_definition else None

        if expected_context_type and isinstance(context_data, expected_context_type):
            return asdict(context_data)

        if is_dataclass(context_data):
            if expected_context_type is None:
                raise EmailTemplateContextTypeError(
                    f"Cannot resolve expected context dataclass for template '{template_key}'."
                )
            raise EmailTemplateContextTypeError(
                f"Template '{template_key}' expects context type "
                f"'{expected_context_type.__name__}', got "
                f"'{context_data.__class__.__name__}'."
            )

        raise EmailTemplateContextTypeError("context_data must be the template's context dataclass instance.")
