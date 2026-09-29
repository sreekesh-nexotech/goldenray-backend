"""What bom registers with other registries (called from ``BomConfig.ready``).

* ``catalog.services.usage`` — ``bom.fixed_items`` (live fixed items of live templates naming the component) and
  ``bom.package_profiles`` (the default battery of a package profile): such a component cannot be deleted (retire it).
"""

from __future__ import annotations

from bom.models import FixedItem, PackageProfile


def fixed_item_usage(component):
    for row in FixedItem.objects.filter(component=component, template__deleted_at__isnull=True).select_related("template"):
        yield {"object_type": "bom.fixeditem", "object_uid": row.uid, "label": f"{row.template.system_type} template: {row.name}"}


def profile_usage(component):
    for profile in PackageProfile.objects.filter(battery_component=component):
        yield {"object_type": "bom.packageprofile", "object_uid": profile.uid, "label": f"Package profile {profile.key}"}


def register() -> None:
    from catalog.services import usage

    usage.register("bom.fixed_items")(fixed_item_usage)
    usage.register("bom.package_profiles")(profile_usage)
