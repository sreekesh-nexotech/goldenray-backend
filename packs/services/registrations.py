"""What packs registers with other registries (called from ``PacksConfig.ready``).

* ``catalog.services.usage`` — ``packs.config_versions`` (a component in a pack BOM or pin of the open draft or the
  current approved version) and ``packs.current_release`` (a component in the current PackRelease's BOMs): such a
  component cannot be deleted, only retired;
* ``core.dashboard`` — counters for the ``packs`` module.

The EMI ``PACK_RELEASE`` price source is not registered here: EMI reads the release itself through
``packs.services.public.emi_size_packs`` (``emi.services.pack_release``), so the dependency points from website
content to configuration, the direction the import-linter contracts allow.
"""

from __future__ import annotations

from packs.models import CURRENT_STATUSES, OPEN_STATUSES, ConfigLine, ConfigPin, ConfigVersion, ReleasePack
from packs.services.releases import current_release


def config_usage(component):
    statuses = (*OPEN_STATUSES, *CURRENT_STATUSES)
    versions = {}
    for version_id, number, uid, status in ConfigLine.objects.filter(component=component, pack__deleted_at__isnull=True, pack__config_version__status__in=statuses).values_list(
        "pack__config_version_id", "pack__config_version__number", "pack__config_version__uid", "pack__config_version__status"
    ):
        versions[version_id] = (number, uid, status)
    for version_id, number, uid, status in ConfigPin.objects.filter(component=component, pack__deleted_at__isnull=True, pack__config_version__status__in=statuses).values_list(
        "pack__config_version_id", "pack__config_version__number", "pack__config_version__uid", "pack__config_version__status"
    ):
        versions[version_id] = (number, uid, status)
    for number, uid, status in sorted(versions.values()):
        yield {"object_type": "packs.config_version", "object_uid": uid, "label": f"Pack config v{number}", "status": status}


def release_usage(component):
    release = current_release()
    if release is None:
        return
    if ReleasePack.objects.filter(release=release, bom__contains=[{"sku": component.sku}]).exists():
        yield {"object_type": "packs.release", "object_uid": release.uid, "label": f"PackRelease #{release.number}", "status": release.status}


def pack_counts(user) -> dict[str, int]:
    release = current_release()
    return {
        "current_release": release.number if release else 0,
        "open_drafts": ConfigVersion.objects.filter(status__in=OPEN_STATUSES).count(),
        "submitted": ConfigVersion.objects.filter(status="SUBMITTED").count(),
    }


def register() -> None:
    from catalog.services import usage
    from core import dashboard

    usage.register("packs.config_versions")(config_usage)
    usage.register("packs.current_release")(release_usage)
    dashboard.register("packs")(pack_counts)
