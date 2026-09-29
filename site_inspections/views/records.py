"""Field records of ``site-inspections/<uid>/``: photos, annotations, equipment checklists, observations."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from flarize.pagination import StandardPagination
from site_inspections.models.choices import EquipmentType
from site_inspections.serializers.records import (
    AnnotationCreateSerializer,
    AnnotationSerializer,
    EquipmentAssessmentSerializer,
    EquipmentPutSerializer,
    EquipmentReviewSerializer,
    InspectionPhotoSerializer,
    InspectionPhotoUploadSerializer,
    ObservationCreateSerializer,
    ObservationSerializer,
)
from site_inspections.services import annotations, equipment, photos, work

TAGS = ["site-inspections"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
READ = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
UID = "[0-9a-fA-F-]{36}"
EQUIPMENT = "|".join(EquipmentType.values)
RECORD_PERMISSIONS = {
    "photos": "view",
    "upload_photo": "edit",
    "delete_photo": "edit",
    "annotations": "view",
    "save_annotation": "edit",
    "equipment": "view",
    "equipment_item": "view",
    "put_equipment": "edit",
    "review_equipment": "approve",
    "observations": "view",
    "add_observation": "edit",
}


class RecordActionsMixin:
    def _page(self, queryset, serializer_class):
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset, self.request, view=self)
        return paginator.get_paginated_response(serializer_class(page, many=True, context=self.get_serializer_context()).data)

    # ── photos ──────────────────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_photos_list", responses={200: InspectionPhotoSerializer(many=True), **READ}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="photos", filter_backends=[], pagination_class=StandardPagination)
    def photos(self, request, *args, **kwargs):
        return self._page(photos.photos_queryset(self.get_object()), InspectionPhotoSerializer)

    @extend_schema(
        operation_id="site_inspections_photos_upload",
        request={"multipart/form-data": InspectionPhotoUploadSerializer},
        responses={201: InspectionPhotoSerializer, **ERRORS, 413: ErrorSerializer},
        tags=TAGS,
        description="Private upload (JPEG/PNG/WebP/HEIC ≤ 15 MB, type sniffed from the bytes).",
    )
    @photos.mapping.post
    def upload_photo(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = InspectionPhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        photo = photos.upload_photo(inspection, user=request.user, file=data["file"], photo_type=data["photo_type"], stage=data.get("stage"), caption=data.get("caption", ""))
        return Response(InspectionPhotoSerializer(photo, context=self.get_serializer_context()).data, status=http_status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="site_inspections_photos_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **ERRORS}, tags=TAGS, description="409 `photo_in_use` while referenced."
    )
    @action(detail=True, methods=["delete"], url_path=f"photos/(?P<photo_uid>{UID})")
    def delete_photo(self, request, *args, photo_uid=None, **kwargs):
        photo = photos.find(self.get_object(), photo_uid)
        photos.delete_photo(photo, user=request.user, expected_version=self.expected_version())
        return Response(status=http_status.HTTP_204_NO_CONTENT)

    # ── annotations ─────────────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_annotations_list", responses={200: AnnotationSerializer(many=True), **READ}, tags=TAGS, description="Current and earlier rectangles.")
    @action(detail=True, methods=["get"], url_path="annotations", filter_backends=[], pagination_class=StandardPagination)
    def annotations(self, request, *args, **kwargs):
        current = request.query_params.get("current") in ("1", "true")
        return self._page(annotations.annotations_queryset(self.get_object(), current_only=current), AnnotationSerializer)

    @extend_schema(
        operation_id="site_inspections_annotations_save",
        request=AnnotationCreateSerializer,
        responses={201: AnnotationSerializer, **ERRORS},
        tags=TAGS,
        description="Saves a new current rectangle of the type (the previous one is kept as history) and the layout measurements. `expected_version` is the inspection's.",
    )
    @annotations.mapping.post
    def save_annotation(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = AnnotationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        annotation = annotations.save_annotation(inspection, user=request.user, expected_version=data.pop("expected_version", None), **data)
        return Response(AnnotationSerializer(annotation).data, status=http_status.HTTP_201_CREATED)

    # ── equipment ───────────────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_equipment_list", responses={200: EquipmentAssessmentSerializer(many=True), **READ}, tags=TAGS, description="The checklists the system type requires.")
    @action(detail=True, methods=["get"], url_path="equipment", filter_backends=[], pagination_class=None)
    def equipment(self, request, *args, **kwargs):
        return Response(EquipmentAssessmentSerializer(equipment.assessments(self.get_object()), many=True).data)

    @extend_schema(operation_id="site_inspections_equipment_retrieve", responses={200: EquipmentAssessmentSerializer, **READ}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path=f"equipment/(?P<equipment_type>{EQUIPMENT})", filter_backends=[], pagination_class=None)
    def equipment_item(self, request, *args, equipment_type=None, **kwargs):
        found = [a for a in equipment.assessments(self.get_object()) if a.equipment_type == equipment_type]
        if not found:
            raise NotFound("equipment_not_required", "This system type does not use this checklist.")
        return Response(EquipmentAssessmentSerializer(found[0]).data)

    @extend_schema(
        operation_id="site_inspections_equipment_put",
        request=EquipmentPutSerializer,
        responses={200: EquipmentAssessmentSerializer, **ERRORS},
        tags=TAGS,
        description="Full replace validated against the check definition; status recomputed over every check. `expected_version` is the checklist's.",
    )
    @equipment_item.mapping.put
    def put_equipment(self, request, *args, equipment_type=None, **kwargs):
        inspection = self.get_object()
        serializer = EquipmentPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        assessment = equipment.put_assessment(inspection, user=request.user, equipment_type=equipment_type, data=data, expected_version=data.pop("expected_version", None))
        return Response(EquipmentAssessmentSerializer(assessment).data)

    @extend_schema(
        operation_id="site_inspections_equipment_review",
        request=EquipmentReviewSerializer,
        responses={200: EquipmentAssessmentSerializer, **ERRORS},
        tags=TAGS,
        description="RESOLVED or WAIVED with a note.",
    )
    @action(detail=True, methods=["post"], url_path=f"equipment/(?P<equipment_type>{EQUIPMENT})/review")
    def review_equipment(self, request, *args, equipment_type=None, **kwargs):
        inspection = self.get_object()
        serializer = EquipmentReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        assessment = equipment.review_assessment(
            inspection, user=request.user, equipment_type=equipment_type, decision=data["decision"], note=data["note"], expected_version=data.get("expected_version")
        )
        return Response(EquipmentAssessmentSerializer(assessment).data)

    # ── observations ────────────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_observations_list", responses={200: ObservationSerializer(many=True), **READ}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="observations", filter_backends=[], pagination_class=StandardPagination)
    def observations(self, request, *args, **kwargs):
        return self._page(work.observations_queryset(self.get_object()), ObservationSerializer)

    @extend_schema(operation_id="site_inspections_observations_create", request=ObservationCreateSerializer, responses={201: ObservationSerializer, **ERRORS}, tags=TAGS)
    @observations.mapping.post
    def add_observation(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = ObservationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        observation = work.add_observation(inspection, user=request.user, note=data["note"], category=data["category"], stage=data.get("stage"), photo_uids=data.get("photo_uids", ()))
        return Response(ObservationSerializer(observation).data, status=http_status.HTTP_201_CREATED)
