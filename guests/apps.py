from django.apps import AppConfig


class GuestsConfig(AppConfig):
    name = "guests"
    verbose_name = "Guests"

    def ready(self):
        # Registers the wedding's email templates with `messaging` (which never imports
        # from here) and connects the bounce receiver. Both happen once, on import.
        import guests.email_contexts  # noqa: F401
        import guests.signals  # noqa: F401
