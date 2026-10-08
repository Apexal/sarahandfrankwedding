from django.urls import path

from pages import views

app_name = "pages"
urlpatterns = [
    path("", views.home, name="home"),
    path("welcome/", views.welcome, name="welcome"),
    path("schedule/", views.schedule, name="schedule"),
    path("registry/", views.registry, name="registry"),
    path("faq/", views.faq, name="faq"),
    path("photos/", views.photos, name="photos"),
    # Admin-editable prose pages. Must stay last: it matches any single path segment,
    # which is why `RESERVED_PAGE_SLUGS` in pages/models.py lists every fixed route.
    path("<slug:slug>/", views.page, name="page"),
]
