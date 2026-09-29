from django.db import models
from django.db.models import Q


class Engine(models.TextChoices):
    CHECKER = "engineeringChecker", "BOM checker (PBC rules)"
    VALIDATION = "engineeringValidation", "Engineering validation (ENG rules)"


class SubjectType(models.TextChoices):
    PACK_CONFIG_VERSION = "PACK_CONFIG_VERSION", "Pack config version"
    PROJECT_BOM = "PROJECT_BOM", "Project BOM"
    QUOTATION_DRAFT = "QUOTATION_DRAFT", "Quotation draft"


class RunResult(models.TextChoices):
    PASS = "PASS", "Pass"
    WARN = "WARN", "Warnings"
    FAIL = "FAIL", "Blocked"


class Severity(models.TextChoices):
    BLOCK = "BLOCK", "Block"
    WARN = "WARN", "Warning"
    INFO = "INFO", "Info"


def in_choices(field: str, choices) -> Q:
    return Q(**{f"{field}__in": list(choices.values)})
