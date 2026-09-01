"""System checks for infrastructure that fails silently when misconfigured.

Storage is the motivating case: a wrong *setting name* is not an error to Django
or to boto3. `endpoint_url` simply stays None, boto3 falls back to real AWS, and
every upload dies with an unresolvable `{bucket}.s3.{region}.amazonaws.com` host
at request time — in production, on the first patient upload, not at deploy.
"""

from django.conf import settings
from django.core.checks import Error, register


@register()
def s3_storage_is_fully_configured(app_configs, **kwargs):
    """Fail the deploy if the default storage is S3 but points nowhere usable."""
    backend = settings.STORAGES.get("default", {}).get("BACKEND", "")
    if not backend.endswith("s3.S3Storage"):
        return []

    # Import here: at module import time the app registry isn't ready yet.
    from django.core.files.storage import storages

    storage = storages["default"]
    errors = []

    if not storage.endpoint_url:
        errors.append(
            Error(
                "S3 storage is active but endpoint_url is unset, so boto3 will "
                "target real AWS instead of Tigris.",
                hint=(
                    "django-storages reads AWS_S3_ENDPOINT_URL. Fly's Tigris "
                    "integration injects an env var named AWS_ENDPOINT_URL_S3 — "
                    "the two are easy to transpose, and the wrong one is a "
                    "no-op. See the ENV == 'production' block in settings.py."
                ),
                id="core.E001",
            )
        )

    if not storage.bucket_name:
        errors.append(
            Error(
                "S3 storage is active but no bucket is configured.",
                hint="Set the BUCKET_NAME env var (fly secrets set BUCKET_NAME=...).",
                id="core.E002",
            )
        )

    if not (storage.access_key and storage.secret_key):
        errors.append(
            Error(
                "S3 storage is active but credentials are missing, so every "
                "upload will fail with a 403.",
                hint=(
                    "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. "
                    "`fly storage create` sets both on the app for you."
                ),
                id="core.E003",
            )
        )

    return errors
