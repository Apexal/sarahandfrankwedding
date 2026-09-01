from django.apps import AppConfig


class AgreementsConfig(AppConfig):
    name = "agreements"
    verbose_name = "Agreements"

    def ready(self):
        import agreements.signals  # noqa: F401  — busts the required-version cache on publish
