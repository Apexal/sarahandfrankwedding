import markdown
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

MARKDOWN_EXTENSIONS = [
    "extra",  # tables, definition lists, footnotes — legal text uses all three
    "sane_lists",
    "smarty",
]


@register.filter
def render_markdown(value: str) -> str:
    """Render agreement Markdown to HTML.

    Raw HTML in the source is escaped before conversion, so only Markdown's own
    constructs produce markup and the `mark_safe` below can never emit an author's
    tags. That matters because these pages are reachable without signing in
    (`agreements:current`) and the ability to write `content_markdown` is an ordinary
    admin permission — one `core/roles.py` group could otherwise be handed a stored
    XSS primitive aimed at every visitor and at superusers.

    The cost is that agreement text cannot embed literal HTML, and markup shown
    inside a code block renders double-escaped. Neither matters for legal prose; if
    raw HTML is ever genuinely needed, sanitize with nh3/bleach rather than dropping
    the escape.
    """
    if not value:
        return ""
    # A fresh converter per call: markdown.Markdown instances carry per-document
    # state and are not safe to share across threaded workers.
    return mark_safe(markdown.markdown(escape(value), extensions=MARKDOWN_EXTENSIONS, output_format="html"))
