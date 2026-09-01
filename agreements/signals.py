from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from agreements.services import invalidate_required_version_cache


@receiver(post_save, sender="agreements.Agreement")
@receiver(post_delete, sender="agreements.Agreement")
@receiver(post_save, sender="agreements.AgreementVersion")
@receiver(post_delete, sender="agreements.AgreementVersion")
def clear_required_version_cache(sender, instance, **kwargs):
    """Drop the cached required-version list whenever agreements change.

    Busts twice, deliberately. These signals fire inside the publish transaction, so
    the immediate delete is what keeps this worker from serving its own stale value —
    but on its own it opens a worse window: another worker can miss the cache before
    COMMIT, re-read the pre-publish state, and pin it for the full TTL with no further
    invalidation coming. The on_commit bust closes that window. (Outside an atomic
    block on_commit runs immediately, so the second call is simply a no-op repeat.)
    """
    invalidate_required_version_cache()
    transaction.on_commit(invalidate_required_version_cache)
