from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand

from core.models import User
from pages.models import WeddingSettings


def mark_email_verified(user):
    """Create the verified allauth EmailAddress row for a seeded user.

    ACCOUNT_EMAIL_VERIFICATION is "mandatory", so without this the seeded
    account could not log in.
    """
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )


class Command(BaseCommand):
    help = "Create the local admin superuser and the wedding settings singleton."

    def add_arguments(self, parser):
        parser.add_argument(
            "--superuser-email",
            type=str,
            default="admin@localhost",
            help="Email for the superuser (default: admin@localhost).",
        )
        parser.add_argument(
            "--superuser-password",
            type=str,
            default="admin",
            help="Password for the superuser (default: admin).",
        )

    def handle(self, *args, **options):
        su_email = options["superuser_email"]
        su_password = options["superuser_password"]

        if User.objects.filter(username="admin").exists():
            self.stdout.write(self.style.WARNING("Superuser 'admin' already exists, skipping."))
        else:
            superuser = User.objects.create_superuser(
                username="admin",
                email=su_email,
                password=su_password,
                first_name="Admin",
                last_name="User",
            )
            mark_email_verified(superuser)
            self.stdout.write(self.style.SUCCESS(f"Created superuser: admin / {su_password}"))

        WeddingSettings.load()
        self.stdout.write(self.style.SUCCESS("Wedding settings singleton is in place."))
