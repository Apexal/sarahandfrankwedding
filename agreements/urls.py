from django.urls import path

from agreements.views import agreement_history, current_agreement, pending_agreements, version_detail

app_name = "agreements"

urlpatterns = [
    path("pending/", pending_agreements, name="pending"),
    path("history/", agreement_history, name="history"),
    path("versions/<int:version_id>/", version_detail, name="version_detail"),
    # Keep last: a bare slug would otherwise shadow the routes above (which also
    # means an Agreement may not use "pending", "history", or "versions" as its key).
    path("<slug:key>/", current_agreement, name="current"),
]
