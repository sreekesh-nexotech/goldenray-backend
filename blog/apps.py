from django.apps import AppConfig


class BlogConfig(AppConfig):
    name = "blog"
    label = "blog"
    verbose_name = "Blog"

    def ready(self):
        from blog.models import Author, Entry, EntryImage, EntrySeo
        from blog.services import dashboard  # noqa: F401 - registers the ``blogs`` dashboard counters
        from media import usage

        # media.usage refuses deleting an asset while a live blog row references it (no silently lost images).
        usage.register(Entry, "cover_image")
        usage.register(EntryImage, "media_asset")
        usage.register(Author, "avatar")
        usage.register(EntrySeo, "og_image")
