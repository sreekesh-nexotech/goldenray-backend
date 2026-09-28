"""Celery tasks owned by media (enqueued only inside ``transaction.on_commit``)."""

from celery import shared_task

from media.services.storage import StorageError


@shared_task(name="media.tasks.generate_thumbnail", ignore_result=True, autoretry_for=(StorageError,), retry_backoff=True, retry_backoff_max=300, max_retries=5)
def generate_thumbnail(asset_uid: str) -> str | None:
    from media.services.thumbnails import generate

    return generate(asset_uid)


@shared_task(name="media.tasks.delete_stored_files", ignore_result=True, autoretry_for=(StorageError,), retry_backoff=True, retry_backoff_max=600, max_retries=8)
def delete_stored_files(visibility: str, keys: list[str]) -> int:
    """Remove a deleted asset's stored objects (original + thumbnail). Missing objects count as removed."""
    from media.services.storage import storage_for

    storage = storage_for(visibility)
    for key in keys:
        storage.delete(key)
    return len(keys)
