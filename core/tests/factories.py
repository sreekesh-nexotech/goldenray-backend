import factory

from core.models import FeatureFlag, OutboxEvent


class FeatureFlagFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FeatureFlag

    key = "LEGACY_API_SHIM"
    enabled = False


class OutboxEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OutboxEvent

    event_type = "tests.something_happened"
    payload = factory.LazyFunction(dict)
