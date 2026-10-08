from django.conf import settings
from django.http import HttpRequest

from core.constants import COMPANY


def global_context(request: HttpRequest):
    return {
        "debug": settings.DEBUG,
        "production": settings.ENV == "production",
        "environment": settings.ENV,
        "company": COMPANY,
        "app_version": settings.APP_VERSION,
    }
