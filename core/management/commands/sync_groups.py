from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from core import roles


class Command(BaseCommand):
    help = "Create/update auth Groups to exactly match core.roles.GROUP_PERMISSIONS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete groups that exist in the database but are not declared in GROUP_PERMISSIONS.",
        )

    def handle(self, *args, **options):
        try:
            undeclared = roles.sync_groups()
        except LookupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Synced {len(roles.GROUP_PERMISSIONS)} group(s)."))

        if not undeclared:
            return
        if options["prune"]:
            Group.objects.filter(name__in=undeclared).delete()
            self.stdout.write(self.style.SUCCESS(f"Pruned undeclared group(s): {', '.join(sorted(undeclared))}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Undeclared group(s) in database (add to GROUP_PERMISSIONS or delete with --prune): "
                    + ", ".join(sorted(undeclared))
                )
            )
