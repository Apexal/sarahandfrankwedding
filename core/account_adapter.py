from urllib.parse import urlsplit

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context as allauth_context
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.text import slugify

from messaging.models import OutboundEmail
from messaging.tasks import send_outbound_email_task


class SiteIdentity:
    """What allauth's stock email templates mean by `current_site`.

    Every template under `allauth/templates/account/email/` extends
    `base_message.txt`, which opens "Hello from {{ current_site.name }}!" and
    signs off with `{{ current_site.domain }}`. allauth fills that in with
    `django.contrib.sites.shortcuts.get_current_site`, which is not usable here
    for two reasons: the sites framework isn't installed, so it falls back to a
    `RequestSite` whose `name` *is* the raw host — every mail would open "Hello
    from myapp.fly.dev!" — and it dereferences the request, so it raises
    outright when there isn't one (a management command, a background task).

    `name` reads `WeddingSettings.couple_names`, so the mail is signed the way the
    site is. `domain` is derived from `APP_BASE_URL` rather than `request.get_host()`
    so a verification mail says the same thing however it was triggered — the host
    a request happens to arrive on is not the address to print in an email.
    """

    def __init__(self):
        # Lazy: this module is imported while the app registry is still populating.
        from pages.models import WeddingSettings

        self.name = WeddingSettings.load().couple_names
        self.domain = urlsplit(settings.APP_BASE_URL).netloc or settings.APP_BASE_URL

    def __str__(self):
        return self.name


class CoreAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # No guest accounts, ever: the only users are the two of us, created by seed/createsuperuser.
        return False

    def send_mail(self, template_prefix, email, context):
        """Route allauth transactional email through the messaging pipeline.

        Rendering still uses allauth's file-based templates, but delivery goes
        through an OutboundEmail audit row and the rate-limited "email" queue
        instead of a synchronous backend send — so password resets and
        verification emails show up in the same audit trail as everything else.

        The context has to be seeded here rather than left to the caller:
        overriding `send_mail` bypasses the base implementation, which is the
        only place allauth adds `request`/`email`/`current_site` to what the
        callers pass in. Without them `base_message.txt` renders "Hello from !"
        — a blank where the product's name belongs, in every allauth email.
        Caller keys win, matching the `ctx.update(context)` upstream.
        """
        context = {
            "request": allauth_context.request,
            "email": email,
            "current_site": SiteIdentity(),
            **context,
        }
        message = self.render_mail(template_prefix, email, context)
        html_body = ""
        if isinstance(message, EmailMultiAlternatives):
            for content, mimetype in message.alternatives:
                if mimetype == "text/html":
                    html_body = content
                    break

        outbound_email = OutboundEmail.objects.create(
            to_email=email,
            from_email=message.from_email or "",
            subject_rendered=message.subject,
            body_text_rendered=message.body,
            body_html_rendered=html_body,
            # e.g. "account/email/password_reset_key" -> "password_reset_key"
            template_key=slugify(template_prefix.rsplit("/", 1)[-1]),
        )
        send_outbound_email_task.enqueue(outbound_email_id=outbound_email.pk)
