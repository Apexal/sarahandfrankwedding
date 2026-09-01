from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Platform"

    def ready(self):
        import core.checks  # noqa: F401
        import core.signals  # noqa: F401
