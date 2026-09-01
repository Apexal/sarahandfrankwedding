import json

import posthog
from auditlog.mixins import AuditlogHistoryAdminMixin
from django import forms
from django.contrib import admin, messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template import Context, Template, TemplateSyntaxError
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join, strip_tags

from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.tasks import send_outbound_email_task
from messaging.template_definitions import (
    get_context_default_values,
    get_context_variable_names,
    get_template_definition,
)


class EmailTemplateVersionAdminForm(forms.ModelForm):
    class Meta:
        model = EmailTemplateVersion
        fields = [
            "template_key",
            "subject_template",
            "html_body_template",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.fields.get("template_key"):
            self.fields["template_key"].widget = forms.Select(choices=EmailTemplateVersion.template_key_choices())


@admin.register(EmailTemplateVersion)
class EmailTemplateVersionAdmin(AuditlogHistoryAdminMixin, admin.ModelAdmin):
    form = EmailTemplateVersionAdminForm
    show_auditlog_history_link = True

    def history_view(self, request, object_id, extra_context=None):
        return redirect(reverse("admin:messaging_emailtemplateversion_auditlog", args=[object_id]))

    change_form_template = "admin/messaging/emailtemplateversion/change_form.html"
    list_display = ("template_key", "template_name", "updated_at")
    search_fields = ("template_key", "subject_template", "html_body_template")
    ordering = ("template_key",)
    readonly_fields = (
        "template_name",
        "available_context_variables",
        "created_at",
        "updated_at",
    )
    change_fieldsets = (
        (
            "Template",
            {
                "fields": (
                    "template_name",
                    "available_context_variables",
                    "subject_template",
                    "html_body_template",
                ),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )
    add_fieldsets = (
        (
            "Template",
            {
                "fields": ("template_key",),
            },
        ),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        return self.change_fieldsets

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "preview/",
                self.admin_site.admin_view(self.preview_view),
                name="messaging_emailtemplateversion_preview",
            ),
        ]
        return custom_urls + urls

    def render_change_form(self, request, context, *args, **kwargs):
        context["preview_url"] = reverse("admin:messaging_emailtemplateversion_preview")
        obj = kwargs.get("obj")
        context["current_template_key"] = obj.template_key if obj and obj.pk else ""
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description="Available context variables")
    def available_context_variables(self, obj):
        if not obj:
            return "-"
        context_variables = get_context_variable_names(obj.template_key)
        if not context_variables:
            return "(No variables)"
        return format_html_join(
            ", ",
            "<code>{}</code>",
            ((f"{{{{ {variable_name} }}}}",) for variable_name in context_variables),
        )

    def preview_view(self, request):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed."}, status=405)

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError, UnicodeDecodeError:
            return JsonResponse({"error": "Invalid request payload."}, status=400)

        template_key = payload.get("template_key") or ""
        if not template_key:
            return JsonResponse({"error": "Template key is required."}, status=400)

        preview_context = get_context_default_values(template_key)

        html = self._render_template(payload.get("html_body_template", ""), preview_context)
        definition = get_template_definition(template_key)
        subject_template = payload.get("subject_template", "")
        subject = self._render_template(subject_template, preview_context).strip()
        if not subject:
            subject = definition.name if definition else template_key
        result = {
            "subject": subject,
            "html": html,
            "text": strip_tags(html) if html else "",
        }
        return JsonResponse(result)

    def _render_template(self, template_source, context_data):
        if not template_source:
            return ""
        try:
            template = Template(template_source)
            return template.render(Context(context_data))
        except TemplateSyntaxError as exc:
            return f"[TemplateSyntaxError] {exc}"
        except Exception as exc:  # pragma: no cover - defensive fallback
            return f"[RenderError] {exc}"


@admin.register(OutboundEmail)
class OutboundEmailAdmin(admin.ModelAdmin):
    list_display = ("created_at", "to_email", "template_key", "status", "sent_at")
    list_filter = ("status", "template_key")
    search_fields = ("to_email", "template_key", "subject_rendered", "error_message")
    ordering = ("-created_at",)
    actions = ["requeue_emails"]

    @admin.action(description="Requeue selected emails for sending")
    def requeue_emails(self, request, queryset):
        eligible = queryset.exclude(status=OutboundEmail.DeliveryStatus.SENT)
        skipped_count = queryset.count() - eligible.count()
        requeued_count = 0
        for outbound_email in eligible:
            outbound_email.status = OutboundEmail.DeliveryStatus.QUEUED
            outbound_email.error_message = ""
            outbound_email.save(update_fields=("status", "error_message", "updated_at"))
            send_outbound_email_task.enqueue(outbound_email_id=outbound_email.pk)
            requeued_count += 1
        if requeued_count:
            posthog.capture("outbound_emails_requeued", properties={"count": requeued_count})
            self.message_user(request, f"Requeued {requeued_count} email(s).", messages.SUCCESS)
        if skipped_count:
            self.message_user(request, f"Skipped {skipped_count} already-sent email(s).", messages.WARNING)

    autocomplete_fields = ("template_version",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "to_email",
        "from_email",
        "reply_to_email",
        "subject_rendered",
        "html_body_preview",
        "body_html_rendered",
        "body_text_rendered",
        "status",
        "error_message",
        "sent_at",
        "send_context",
        "template_key",
        "template_version",
    )
    fieldsets = (
        (
            "Email",
            {
                "fields": (
                    "to_email",
                    "from_email",
                    "reply_to_email",
                    "subject_rendered",
                    "html_body_preview",
                    "body_html_rendered",
                    "body_text_rendered",
                ),
            },
        ),
        (
            "Delivery",
            {
                "fields": (
                    "status",
                    "error_message",
                    "sent_at",
                ),
            },
        ),
        (
            "Template",
            {
                "fields": (
                    "template_key",
                    "template_version",
                    "send_context",
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Rendered HTML preview")
    def html_body_preview(self, obj):
        if not obj.body_html_rendered:
            return "(No HTML body rendered)"
        return format_html(
            '<iframe srcdoc="{}" '
            'style="width: 100%; min-height: 420px; border: 1px solid #ccc; background: #fff;" '
            'sandbox="allow-same-origin"></iframe>',
            obj.body_html_rendered,
        )
