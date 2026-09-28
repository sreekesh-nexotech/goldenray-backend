from django.apps import AppConfig


class CompanyConfig(AppConfig):
    name = "company"
    label = "company"
    verbose_name = "Company"

    def ready(self):
        from company.models import CompanyProfile
        from company.services.integrations import stored_config
        from company.services.profile import ASSET_RULES
        from core import integrations
        from media import usage

        # Platform code (Bunny uploads, SMTP delivery, Twilio Verify) reads Admin-stored provider settings through
        # core.integrations without importing this app.
        integrations.register_resolver(stored_config)
        for field in ASSET_RULES:
            usage.register(CompanyProfile, field)
