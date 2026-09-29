from decimal import Decimal

import factory

from catalog.tests.factories import ComponentFactory
from procurement.models import Batch, BatchCharge, BatchLine, Supplier


class SupplierFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Supplier

    code = factory.Sequence(lambda n: f"SUP{n:03d}")
    name = factory.Sequence(lambda n: f"Supplier {n}")


class BatchFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Batch

    number = factory.Sequence(lambda n: f"BATCH-2026-T{n:03d}")
    supplier = factory.SubFactory(SupplierFactory)
    invoice_no = factory.Sequence(lambda n: f"INV-{n}")


class BatchLineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BatchLine

    batch = factory.SubFactory(BatchFactory)
    component = factory.SubFactory(ComponentFactory)
    qty = Decimal("100.000")
    unit_purchase_price = Decimal("3710.00")


class BatchChargeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BatchCharge

    batch = factory.SubFactory(BatchFactory)
    kind = "FREIGHT"
    amount = Decimal("5000.00")
