import re

from django import template
from django_ckeditor_5.widgets import CKEditor5Widget

register = template.Library()


@register.simple_tag(takes_context=True)
def ckeditor_media(context):
    """Render the CKEditor widget CSS/JS, at most once per request.

    Place this next to every template fragment that renders a CKEditor field
    instead of rendering `{{ form.media }}` — duplicate calls collapse to a
    single include. Executing bundle.js twice resets window.editors and
    ckeditorRegisterCallback, orphaning any JS hooks attached to the first
    bundle's registry.

    The guard lives on the request (not render_context) so it survives
    {% include %} boundaries, which get isolated render_context state.
    """
    request = context.get("request")
    if request is not None:
        if getattr(request, "_ckeditor_media_rendered", False):
            return ""
        request._ckeditor_media_rendered = True
    return CKEditor5Widget().media.render()


@register.filter
def format_phone(phone_number):
    """
    Format a phone number in E.164 format (+1234567890) to a human-readable format.
    Examples:
        +13478519465 -> +1 (347) 851-9465
        +442071234567 -> +44 20 7123 4567
    """
    if not phone_number:
        return ""

    # Remove all non-digit characters except the leading +
    clean = re.sub(r"[^\d+]", "", phone_number)

    # Handle US/Canada numbers (+1)
    if clean.startswith("+1") and len(clean) == 12:
        return f"+1 ({clean[2:5]}) {clean[5:8]}-{clean[8:]}"

    # For other formats, just return as-is or with basic formatting
    return clean
