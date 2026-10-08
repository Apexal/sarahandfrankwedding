"""Seed one editable `EmailTemplateVersion` row per wedding template key.

The starter ships no rows and `EmailTemplateService` raises when one is missing, so a
fresh deploy needs these before the first campaign. Bodies come from
`guests/templates/guests/email/<key>.html` and `<key>.subject.txt`; `get_or_create`
leaves rows that already exist (and any admin edits to them) alone, so re-running is
harmless. Reversing keeps the rows for the same reason.
"""

from pathlib import Path

from django.db import migrations

TEMPLATE_KEYS = (
    "save_the_date",
    "address_nudge",
    "rsvp_nudge",
    "announcement",
    "rsvp_confirmation",
    "rsvp_received_internal",
)
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "guests" / "email"


def read_html_body(key: str) -> str:
    return (TEMPLATE_DIR / f"{key}.html").read_text()


def seed_email_templates(apps, schema_editor) -> int:
    """Create the missing rows and return how many were created."""
    EmailTemplateVersion = apps.get_model("messaging", "EmailTemplateVersion")
    created_count = 0
    for key in TEMPLATE_KEYS:
        _, created = EmailTemplateVersion.objects.get_or_create(
            template_key=key,
            defaults={
                "subject_template": (TEMPLATE_DIR / f"{key}.subject.txt").read_text().strip(),
                "html_body_template": read_html_body(key),
            },
        )
        created_count += int(created)
    return created_count


class Migration(migrations.Migration):
    dependencies = [
        ("guests", "0001_initial"),
        ("messaging", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_email_templates, migrations.RunPython.noop),
    ]
