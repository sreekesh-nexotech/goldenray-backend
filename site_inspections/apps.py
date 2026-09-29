from django.apps import AppConfig


class SiteInspectionsConfig(AppConfig):
    name = "site_inspections"
    label = "site_inspections"
    verbose_name = "Site inspections"

    def ready(self):
        from customers.services import merge
        from documents import access
        from media import folders, usage
        from site_inspections.models import Inspection, LocationApproval, Photo
        from site_inspections.services import common, customer_timeline, photos, scoping  # noqa: F401 - registers the scope filters and the timeline provider

        # Inspection photos and approval signatures/scans are served by this context only (never the media library).
        folders.reserve(photos.MEDIA_FOLDER)
        usage.register(Photo, "asset")
        usage.register(LocationApproval, "signature_asset")
        usage.register(LocationApproval, "paper_scan_asset")
        # A customer merge re-points inspections; a customer with inspections cannot be deleted.
        merge.register_dependant(Inspection, "customer", blocks_delete=True)
        # Inspection reports follow the inspection's record scope.
        access.register(common.OBJECT_TYPE, module=common.MODULE, action="view", visible=lambda user, uid: common.visible(user).filter(uid=uid).exists())
