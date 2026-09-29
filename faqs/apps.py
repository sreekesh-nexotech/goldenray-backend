from django.apps import AppConfig


class FaqsConfig(AppConfig):
    name = "faqs"
    label = "faqs"
    verbose_name = "FAQs"

    def ready(self):
        from core import dashboard
        from faqs.models import Faq
        from faqs.services.faqs import dashboard_counts
        from media import usage

        # media.usage refuses deleting an asset while a live FAQ uses it as its social image.
        usage.register(Faq, "og_image")
        dashboard.register("faqs")(dashboard_counts)
