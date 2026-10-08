from pages.models import Page, WeddingSettings


def wedding(request):
    """Expose the WeddingSettings singleton and the nav's prose pages to every template.

    `nav_pages` is a lazy queryset, so templates that never render the nav (the admin,
    emails) cost nothing.
    """
    return {
        "wedding": WeddingSettings.load(),
        "nav_pages": Page.objects.filter(published=True, show_in_nav=True).order_by("nav_order", "title"),
    }
