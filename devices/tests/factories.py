import factory

from core import service_credentials
from core.models import ServiceCredential
from devices.models import Agent, Device, DeviceUser, ProtocolMapping, SyncLog
from hr.tests.factories import OfficeFactory


class AgentFactory(factory.django.DjangoModelFactory):
    """An agent with a live credential; the plaintext token is on ``agent.token``."""

    class Meta:
        model = Agent
        skip_postgeneration_save = True

    code = factory.Sequence(lambda n: f"OFFICE-{n:03d}-AGENT")
    name = factory.Sequence(lambda n: f"Office PC {n}")
    office = factory.SubFactory(OfficeFactory)

    @factory.post_generation
    def with_credential(self, create, extracted, **kwargs):
        if not create or extracted is False:
            self.token = None
            return
        credential, token = service_credentials.issue(ServiceCredential.Kind.AGENT, f"Office agent {self.code}", self)
        Agent.all_objects.filter(pk=self.pk).update(credential=credential)
        self.credential = credential
        self.token = token


class DeviceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Device

    name = factory.Sequence(lambda n: f"TERM-{n:02d}")
    serial_number = factory.Sequence(lambda n: f"NCD{n:010d}")
    expected_serial = factory.LazyAttribute(lambda device: device.serial_number)
    ip_address = factory.Sequence(lambda n: f"192.168.1.{(n % 250) + 2}")
    office = factory.SubFactory(OfficeFactory)
    identity_status = Device.IdentityStatus.VERIFIED


class DeviceUserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DeviceUser

    device = factory.SubFactory(DeviceFactory)
    pin = factory.Sequence(lambda n: str(n + 1))
    name = factory.Sequence(lambda n: f"User {n}")


class SyncLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SyncLog

    device = factory.SubFactory(DeviceFactory)
    sync_type = SyncLog.Type.USERS
    status = SyncLog.Status.SUCCESS


class ProtocolMappingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ProtocolMapping

    field = ProtocolMapping.Field.STATUS
    raw_value = factory.Sequence(lambda n: n)
    meaning_type = ProtocolMapping.MeaningType.VERIFY_MODE
    meaning_code = "FACE"


def users_log(device, pins, *, status=SyncLog.Status.SUCCESS, **kwargs):
    """A USERS sync log (the presence watermark) listing ``pins``."""
    return SyncLogFactory(device=device, status=status, details={"users_present": list(pins)} if status == SyncLog.Status.SUCCESS else {}, **kwargs)
