"""URL configuration for the wedding site.

Order matters at the end: `pages.urls` owns "/" and the /<slug>/ catch-all for
admin-editable prose pages, so it must be the last include.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from core.views import favicon_ico, health, robots_txt

urlpatterns = [
    path("robots.txt", robots_txt, name="robots_txt"),
    path("favicon.ico", favicon_ico, name="favicon_ico"),
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("ckeditor5/", include("django_ckeditor_5.urls")),
    # Resend delivery webhooks (bounces, complaints); the receiver is guests/signals.py.
    path("anymail/", include("anymail.urls")),
    path("save-the-date/", include("guests.urls.save_the_date")),
    path("rsvp/", include("guests.urls.rsvp")),
    path("manage/", include("guests.urls.manage")),
    path("", include("pages.urls")),
]

if settings.DEBUG:
    from debug_toolbar.toolbar import debug_toolbar_urls
    from django.conf.urls.static import static

    urlpatterns = [
        *debug_toolbar_urls(),
        path("__reload__/", include("django_browser_reload.urls")),
        # Serve uploaded media locally; in production it's served from S3/Tigris.
        *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
        *urlpatterns,
    ]
