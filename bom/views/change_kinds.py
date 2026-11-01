"""BOM masters a Project Head changes through a change request (``approvals``): applied with the same serializer and
service as the CRUD endpoints in :mod:`bom.views.masters`, by the Admin who approves."""

from __future__ import annotations

from approvals.services.kinds import viewset_kind
from bom.models import FixedItem, PackageProfile, Slot, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from bom.views import masters


def register_kinds() -> None:
    viewset_kind("bom.template", "BOM template", masters.TemplateViewSet, model=Template)
    viewset_kind("bom.slot", "BOM slot", masters.SlotViewSet, model=Slot)
    viewset_kind("bom.fixed_item", "Fixed price item", masters.FixedItemViewSet, model=FixedItem)
    viewset_kind("bom.structure_template", "Structure template", masters.StructureTemplateViewSet, model=StructureTemplate)
    viewset_kind("bom.structure_item", "Structure rate", masters.StructureItemViewSet, model=StructureTemplateItem)
    viewset_kind("bom.tube_weight", "Tube weight", masters.TubeWeightViewSet, model=TubeWeight)
    viewset_kind("bom.package_profile", "Package profile", masters.PackageProfileViewSet, model=PackageProfile)
