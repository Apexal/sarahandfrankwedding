import random

from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand
from faker import Faker

from core.constants import US_TIMEZONE_CHOICES
from core.models import User


def mark_email_verified(user):
    """Create the verified allauth EmailAddress row for a seeded user.

    ACCOUNT_EMAIL_VERIFICATION is "mandatory", so without this the seeded
    accounts could not log in.
    """
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )


US_TIMEZONES = [tz for tz, _ in US_TIMEZONE_CHOICES]


class Command(BaseCommand):
    help = "Seed the database with a superuser and sample users for local development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--users",
            type=int,
            default=5,
            help="Number of sample users to create (default: 5).",
        )
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
        fake = Faker()
        num_users = options["users"]
        su_email = options["superuser_email"]
        su_password = options["superuser_password"]

        # Create superuser
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

        # Create sample users
        created = 0
        for _ in range(num_users):
            first_name = fake.first_name()
            last_name = fake.last_name()
            username = f"{first_name.lower()}.{last_name.lower()}"
            email = f"{username}@example.com"

            if User.objects.filter(username=username).exists():
                continue

            sample_user = User.objects.create_user(
                username=username,
                email=email,
                password="password",
                first_name=first_name,
                last_name=last_name,
                phone_number=fake.numerify("+1##########"),
                time_zone=random.choice(US_TIMEZONES),
            )
            mark_email_verified(sample_user)
            created += 1

        self.stdout.write(self.style.SUCCESS(f"Created {created} sample user(s)."))
