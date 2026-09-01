"""Code-defined staff groups.

Groups are declared here — not clicked together in the admin — and synced to
the database by `manage.py sync_groups` (run automatically by `make setup`,
`make migrate`, and `entry.sh`). That keeps group permissions identical across
environments and lets tests assert them.

Keys are group names; values are permissions in "app_label.codename" form (the
same form `User.has_perm` takes). Syncing is exact: a permission removed here
is removed from the group in the database on the next sync.

Groups model staff tiers — what an account may do, chiefly in the Django
admin. Domain roles (what kind of person a user is, e.g. patient/clinician)
belong on the User model as a field, with views gated on that field and
querysets filtered per user.

Example:

    GROUP_PERMISSIONS = {
        "Support": [
            "core.view_user",
            "messaging.view_outboundemail",
        ],
    }
"""

from django.contrib.auth.models import Group, Permission

GROUP_PERMISSIONS: dict[str, list[str]] = {}


def sync_groups() -> list[str]:
    """Create or update Groups to exactly match GROUP_PERMISSIONS.

    Returns the names of groups present in the database but not declared here,
    so callers can warn about or prune them. Raises LookupError if a declared
    permission does not exist.
    """
    for name, permissions in GROUP_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set([_resolve_permission(perm) for perm in permissions])
    return list(Group.objects.exclude(name__in=GROUP_PERMISSIONS).values_list("name", flat=True))


def _resolve_permission(perm: str) -> Permission:
    try:
        app_label, codename = perm.split(".", 1)
        return Permission.objects.get(content_type__app_label=app_label, codename=codename)
    except ValueError, Permission.DoesNotExist:
        raise LookupError(
            f'Unknown permission "{perm}" in GROUP_PERMISSIONS — expected "app_label.codename", '
            f'e.g. "core.view_user". Custom permissions must be declared in a model Meta and migrated first.'
        ) from None
