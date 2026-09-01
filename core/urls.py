from django.urls import path

from core.views import home, profile_edit, stop_impersonation

app_name = "core"
urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile_edit, name="profile"),
    path("impersonation/stop/", stop_impersonation, name="stop_impersonation"),
]
