"""
URL configuration for sarahandfrankwedding project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from core.api import api
from core.views import favicon_ico, health, robots_txt

urlpatterns = [
    path("robots.txt", robots_txt, name="robots_txt"),
    path("favicon.ico", favicon_ico, name="favicon_ico"),
    path("health/", health, name="health"),
    # JSON API for the mobile app. The OpenAPI schema at /api/openapi.json is what
    # the Expo client's `npm run gen:api` generates its typed client from.
    path("api/", api.urls),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("agreements/", include("agreements.urls")),
    path("ckeditor5/", include("django_ckeditor_5.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    from debug_toolbar.toolbar import debug_toolbar_urls
    from django.conf.urls.static import static

    urlpatterns += debug_toolbar_urls()
    urlpatterns += [path("__reload__/", include("django_browser_reload.urls"))]
    # Serve uploaded media locally; in production it's served from S3/Tigris.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
