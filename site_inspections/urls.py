"""Site inspections URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``site-inspections/``, ``engineer/site-inspections/``; customer ``inspection-approvals/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from site_inspections.views.customer import ApprovalRespondView, ApprovalSendOtpView, ApprovalSummaryView
from site_inspections.views.inspections import EngineerQueueViewSet, InspectionViewSet
from site_inspections.views.stages import STAGE_VIEWS

router = SimpleRouter(trailing_slash=True)
router.register("engineer/site-inspections", EngineerQueueViewSet, basename="engineer-site-inspections")
router.register("site-inspections", InspectionViewSet, basename="site-inspections")

TOKEN = "<str:token>"

staff_urlpatterns = [
    *[path(f"site-inspections/<uuid:uid>/stages/{key}/", view.as_view(), name=f"site-inspections-stage-{key}") for key, view in STAGE_VIEWS.items()],
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns = [
    path(f"inspection-approvals/{TOKEN}/", ApprovalSummaryView.as_view(), name="inspection-approval"),
    path(f"inspection-approvals/{TOKEN}/send-otp/", ApprovalSendOtpView.as_view(), name="inspection-approval-send-otp"),
    path(f"inspection-approvals/{TOKEN}/respond/", ApprovalRespondView.as_view(), name="inspection-approval-respond"),
]
