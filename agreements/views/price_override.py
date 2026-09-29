"""``agreements/<uid>/price-override/`` — the audited price change of a DRAFT (PLAN D-1 / Plan 2 D2-1).

Answers 404 while the feature flag ``AGREEMENTS_PRICE_OVERRIDE`` is off (a disabled surface is indistinguishable from an
absent one); needs ``agreements.manage`` and a reason; the record scope of ``agreements`` applies.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from agreements.models import Agreement
from agreements.serializers.agreements import AgreementDetailSerializer, PriceOverrideSerializer
from agreements.services.acceptance import FLAG, override_price
from agreements.services.common import agreements_queryset, reload
from agreements.views.agreements import TAGS, WRITE_ERRORS
from core.errors import NotFound
from core.flags import FlagRequiredMixin
from core.views import BaseAPIView


class AgreementPriceOverrideView(FlagRequiredMixin, BaseAPIView):
    required_flag = FLAG
    module = "agreements"
    action_permissions = {"POST": "manage"}

    def _agreement(self, uid) -> Agreement:
        agreement = self.scope_queryset(agreements_queryset()).filter(uid=uid).first()
        if agreement is None:
            raise NotFound("not_found", "Agreement not found.")
        return agreement

    @extend_schema(
        operation_id="agreements_price_override",
        request=PriceOverrideSerializer,
        responses={200: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Set the discount or the final price of a DRAFT (the other follows); agreements.manage, a reason, flag AGREEMENTS_PRICE_OVERRIDE (404 while off). Audited.",
    )
    def post(self, request, uid, *args, **kwargs):
        body = PriceOverrideSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        agreement = override_price(
            self._agreement(uid),
            user=request.user,
            reason=data["reason"],
            discount=data.get("discount"),
            final_price=data.get("final_price"),
            expected_version=data.get("expected_version"),
        )
        return Response(AgreementDetailSerializer(reload(agreement), context={"request": request}).data)
