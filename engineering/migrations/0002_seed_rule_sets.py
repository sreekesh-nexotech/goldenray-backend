"""Seed the two rule registers (docs/decisions/engines-rules.md): PBC ``phase1e.1`` and ENG ``phase1e.eng``, active.

The documents are the engines' registers at migrate time; the seed is skipped for a version that already exists, so a
changed register ships as a new rule-set version, never as an edit of a seeded one.
"""

import uuid

from django.db import migrations
from django.utils import timezone

from engines.engineering_checker import DEFAULT_RULE_SET, VALIDATION_RULE_SET


def seed(apps, schema_editor):
    RuleSet = apps.get_model("engineering", "RuleSet")
    now = timezone.now()
    for rule_set in (DEFAULT_RULE_SET, VALIDATION_RULE_SET):
        document = rule_set.as_json()
        if RuleSet.objects.filter(rules_version=document["version"], deleted_at__isnull=True).exists():
            continue
        RuleSet.objects.create(
            uid=uuid.uuid4(),
            rules_version=document["version"],
            engine=document["engine"],
            rules=document,
            active=not RuleSet.objects.filter(engine=document["engine"], active=True, deleted_at__isnull=True).exists(),
            activated_at=now,
            note="Seeded from the Flarize rule register (engines.engineering_checker).",
        )


def unseed(apps, schema_editor):
    apps.get_model("engineering", "RuleSet").objects.filter(rules_version__in=["phase1e.1", "phase1e.eng"], runs__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("engineering", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
