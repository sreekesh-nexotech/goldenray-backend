"""``GET /api/public/<version>/testimonials/`` — the website's testimonials (PLAN §3.3; replaces
``/bom/api/quotation-testimonials/``): active rows marked ``show_on_website``, in display order; anonymous, throttled
``public_read``, cached under ``quotations_testimonials`` (bumped on every testimonial write) and ``media``."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView

from core.serializers import ErrorSerializer
from core.views import PublicViewMixin
from flarize.cache_utils import cache_response
from quotations.models import Testimonial
from quotations.serializers.content import PublicTestimonialSerializer
from quotations.services import content
from quotations.services.common import PUBLIC_TESTIMONIALS_NAMESPACE

CACHE_TTL = 300


class PublicTestimonialListView(PublicViewMixin, GenericAPIView):
    serializer_class = PublicTestimonialSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation: no database access
            return Testimonial.objects.none()
        return content.public_testimonials()

    @extend_schema(
        operation_id="public_testimonials_list",
        responses={200: PublicTestimonialSerializer(many=True), 429: ErrorSerializer},
        tags=["public"],
        auth=[],
        description="Homeowner testimonials shown on the website (paginated, display order).",
    )
    @cache_response(namespaces=(PUBLIC_TESTIMONIALS_NAMESPACE, "media"), ttl=CACHE_TTL)
    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(PublicTestimonialSerializer(page, many=True).data)
