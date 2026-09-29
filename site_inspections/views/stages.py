"""``PATCH site-inspections/<uid>/stages/<stage>/`` — one view per stage, each with its own allow-list serializer
(so the OpenAPI schema shows exactly the columns of that stage)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseAPIView
from site_inspections.serializers.inspections import STAGE_SERIALIZERS, InspectionSerializer
from site_inspections.services import common, inspections, stages

ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class StageView(BaseAPIView):
    module = common.MODULE
    action_permissions = {"PATCH": "edit"}
    stage_key = ""
    serializer_class = None

    def patch(self, request, *args, uid=None, **kwargs):
        inspection = common.get_visible(request.user, uid)
        serializer = self.serializer_class(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        expected = data.pop("expected_version", None)
        updated = inspections.update_stage(inspection, user=request.user, stage_key=self.stage_key, data=data, expected_version=expected)
        return Response(InspectionSerializer(common.inspections_queryset().get(pk=updated.pk), context={"request": request}).data)


def _view(stage: stages.Stage) -> type[StageView]:
    serializer = STAGE_SERIALIZERS[stage.key]
    name = serializer.__name__.replace("Serializer", "View")

    @extend_schema(
        operation_id=f"site_inspections_stage_{stage.key.replace('-', '_')}",
        request=serializer,
        responses={200: InspectionSerializer, **ERRORS},
        tags=["site-inspections"],
        description=f"Stage {stage.number} — {stage.title}: only these columns; starts the work (→ IN_PROGRESS). `expected_version` is the inspection's.",
    )
    def patch(self, request, *args, **kwargs):
        return StageView.patch(self, request, *args, **kwargs)

    return type(name, (StageView,), {"stage_key": stage.key, "serializer_class": serializer, "patch": patch, "__module__": __name__})


STAGE_VIEWS: dict[str, type[StageView]] = {key: _view(stages.BY_KEY[key]) for key in stages.EDITABLE_KEYS}
