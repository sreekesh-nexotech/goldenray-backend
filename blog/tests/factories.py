"""factory_boy factories for blog records (public numbers come from the same sequences the services use)."""

import factory
from django.utils import timezone

from blog.models import (
    Author,
    Badge,
    Category,
    Collection,
    ContentBlock,
    Entry,
    EntryAttributeValue,
    EntryImage,
    EntrySeo,
    EntrySlugHistory,
    Tag,
    Template,
    TemplateAttributeSlot,
    TemplateImageGroup,
)
from blog.services.common import SEQ_AUTHOR, SEQ_BADGE, SEQ_BLOCK, SEQ_CATEGORY, SEQ_ENTRY, SEQ_TAG


def _public_number(kind):
    def make(_):
        from django.db import transaction

        from core import sequences

        with transaction.atomic():
            return sequences.next_value(kind)

    return factory.LazyAttribute(make)


class CollectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Collection

    api_uid = factory.Sequence(lambda n: f"collection-{n}")
    singular_name = "Article"
    plural_name = factory.Sequence(lambda n: f"Articles {n}")
    path_prefix = factory.Sequence(lambda n: f"/blog-{n}")


class TemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Template

    slug = factory.Sequence(lambda n: f"template-{n}")
    name = factory.Sequence(lambda n: f"Template {n}")


class ImageGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TemplateImageGroup

    template = factory.SubFactory(TemplateFactory)
    key = factory.Sequence(lambda n: f"group{n}")
    label = "Group"


class AttributeSlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TemplateAttributeSlot

    template = factory.SubFactory(TemplateFactory)
    key = factory.Sequence(lambda n: f"slot{n}")
    label = "Slot"


class AuthorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Author

    delivery_id = _public_number(SEQ_AUTHOR)
    name = factory.Sequence(lambda n: f"Author {n}")
    slug = factory.Sequence(lambda n: f"author-{n}")
    role = "Solar Engineer"
    bio = "Designs residential systems."


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Category

    delivery_id = _public_number(SEQ_CATEGORY)
    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")


class TagFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tag

    delivery_id = _public_number(SEQ_TAG)
    name = factory.Sequence(lambda n: f"Tag {n}")
    slug = factory.Sequence(lambda n: f"tag-{n}")


class BadgeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Badge

    delivery_id = _public_number(SEQ_BADGE)
    name = factory.Sequence(lambda n: f"Badge {n}")
    slug = factory.Sequence(lambda n: f"badge-{n}")
    color = "#ED8723"


class EntryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Entry

    delivery_id = _public_number(SEQ_ENTRY)
    collection = factory.SubFactory(CollectionFactory)
    title = factory.Sequence(lambda n: f"Solar guide {n}")
    slug = factory.Sequence(lambda n: f"solar-guide-{n}")
    excerpt = "A practical guide."


class PublishedEntryFactory(EntryFactory):
    status = Entry.Status.PUBLISHED
    published_at = factory.LazyFunction(timezone.now)
    published_on = factory.LazyAttribute(lambda entry: entry.published_at)


class ContentBlockFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ContentBlock

    delivery_id = _public_number(SEQ_BLOCK)
    entry = factory.SubFactory(EntryFactory)
    data = factory.LazyFunction(lambda: [{"type": "paragraph", "children": [{"type": "text", "text": "Body."}]}])


class EntryImageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EntryImage

    entry = factory.SubFactory(EntryFactory)
    group_key = "coverImg"
    external_url = "https://cdn.example.com/cover.png"


class AttributeValueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EntryAttributeValue

    entry = factory.SubFactory(EntryFactory)
    slot_key = "difficulty"
    value = "Beginner"


class EntrySeoFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EntrySeo

    entry = factory.SubFactory(EntryFactory)
    seo_title = "A title"
    meta_description = "A description that is long enough to read well in search results for this entry, ok."


class AliasFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EntrySlugHistory

    entry = factory.SubFactory(PublishedEntryFactory)
    collection = factory.LazyAttribute(lambda alias: alias.entry.collection)
    slug = factory.Sequence(lambda n: f"old-slug-{n}")
