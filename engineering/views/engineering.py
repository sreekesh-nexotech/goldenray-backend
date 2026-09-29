"""``engineering/rule-sets/`` (+ ``activate/``), ``engineering/runs/``, ``engineering/findings/<uid>/acknowledge/``.

``engineering.view`` reads · ``engineering.approve`` activates a rule set and acknowledges (or waives) findings.
Running the checker (``engineering.verify``) is ``packs/config-versions/<uid>/run-checker/``.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from engineering.models import Engine, RuleSet, Run, RunResult, SubjectType
from engineering.serializers import (
    AcknowledgementSerializer,
    AcknowledgeSerializer,
    FindingSerializer,
    RuleSetActivateSerializer,
    RuleSetDetailSerializer,
    RuleSetSerializer,
    RunDetailSerializer,
    RunSerializer,
)
from engineering.services import rule_sets, runs

TAGS = ["engineering"]
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class RuleSetFilter(django_filters.FilterSet):
    engine = django_filters.ChoiceFilter(choices=Engine.choices)
    active = django_filters.BooleanFilter()

    class Meta:
        model = RuleSet
        fields: list[str] = []


class RunFilter(django_filters.FilterSet):
    subject_type = django_filters.ChoiceFilter(choices=SubjectType.choices)
    subject_uid = django_filters.UUIDFilter()
    result = django_filters.ChoiceFilter(choices=RunResult.choices)

    class Meta:
        model = Run
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="engineering_rule_sets_list", tags=TAGS),
    retrieve=extend_schema(operation_id="engineering_rule_sets_retrieve", responses={200: RuleSetDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class RuleSetViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "engineering"
    action_permissions = {"list": "view", "retrieve": "view", "activate": "approve"}
    serializer_class = RuleSetSerializer
    filterset_class = RuleSetFilter
    search_fields = ["rules_version", "note"]
    ordering_fields = ["created_at", "rules_version"]
    ordering = ["engine", "-created_at"]

    def base_queryset(self):
        return rule_sets.rule_sets_queryset()

    def get_serializer_class(self):
        return RuleSetDetailSerializer if self.action == "retrieve" else RuleSetSerializer

    @extend_schema(
        operation_id="engineering_rule_sets_activate",
        request=RuleSetActivateSerializer,
        responses={200: RuleSetSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Make this rule set the ACTIVE one of its engine (the previous one is deactivated).",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def activate(self, request, *args, **kwargs):
        body = RuleSetActivateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rule_set = rule_sets.activate(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"), note=body.validated_data.get("note", ""))
        return Response(RuleSetSerializer(rule_set).data)


@extend_schema_view(
    list=extend_schema(operation_id="engineering_runs_list", tags=TAGS),
    retrieve=extend_schema(operation_id="engineering_runs_retrieve", responses={200: RunDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class RunViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "engineering"
    action_permissions = {"list": "view", "retrieve": "view"}
    serializer_class = RunSerializer
    filterset_class = RunFilter
    search_fields = ["subject_label"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at", "-id"]

    def base_queryset(self):
        return runs.runs_queryset()

    def get_serializer_class(self):
        return RunDetailSerializer if self.action == "retrieve" else RunSerializer


class FindingViewSet(BaseViewSet):
    module = "engineering"
    action_permissions = {"acknowledge": "approve"}
    serializer_class = FindingSerializer

    def base_queryset(self):
        return runs.findings_queryset()

    @extend_schema(
        operation_id="engineering_findings_acknowledge",
        request=AcknowledgeSerializer,
        responses={201: AcknowledgementSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Acknowledge a finding (a BLOCK finding is waived); the reason is required. It counts for the same finding in later runs of the subject.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def acknowledge(self, request, *args, **kwargs):
        body = AcknowledgeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        ack = runs.acknowledge(self.get_object(), user=request.user, reason=body.validated_data["reason"], expected_version=body.validated_data.get("expected_version"))
        return Response(AcknowledgementSerializer(ack).data, status=201)
