"""Grant the new ``change_requests`` registry module to the system roles (seeds.py: a new module arrives by data
migration, since ``seed_roles`` never touches an existing role).

* Super Admin, Admin: every action, scope ``all`` — they review and apply.
* Project Head, Procurement: ``view`` + ``create`` (scope ``owned``), and their direct write grants on catalog,
  pricing and BOM are reduced to ``view`` — those changes now take effect only after an Admin approves (DV-153).
  Procurement also loses ``procurement.commit``: committing or reversing a batch sets prices (DV-155).

Reversing restores the seeded direct grants and removes the module.
"""

from django.db import migrations

FULL = ["view", "create", "approve"]
REQUESTER = ["view", "create"]
#: What the requester roles keep on the modules whose changes now need approval.
REDUCED = {
    "project-head": {"catalog": ["view"], "pricing": ["view"], "bom": ["view"]},
    "procurement": {"catalog": ["view"], "procurement": ["view", "create", "edit"]},
}
#: The seeded direct grants, for the reverse migration.
SEEDED = {
    "project-head": {"catalog": ["view", "create", "edit", "approve"], "pricing": ["view", "edit"], "bom": ["view", "edit"]},
    "procurement": {"catalog": ["view", "create", "edit"], "procurement": ["view", "create", "edit", "commit"]},
}


def forward(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(deleted_at__isnull=True, slug__in=["super-admin", "admin", *REDUCED]):
        permissions = dict(role.permissions or {})
        scopes = dict(role.scopes or {})
        if role.slug in ("super-admin", "admin"):
            permissions["change_requests"] = FULL
            scopes["change_requests"] = "all"
        else:
            permissions.update(REDUCED[role.slug])
            permissions["change_requests"] = REQUESTER
            scopes["change_requests"] = "owned"
        role.permissions = permissions
        role.scopes = scopes
        role.version = (role.version or 0) + 1
        role.save(update_fields=["permissions", "scopes", "version"])


def backward(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(deleted_at__isnull=True, slug__in=["super-admin", "admin", *SEEDED]):
        permissions = dict(role.permissions or {})
        scopes = dict(role.scopes or {})
        permissions.pop("change_requests", None)
        scopes.pop("change_requests", None)
        permissions.update(SEEDED.get(role.slug, {}))
        role.permissions = permissions
        role.scopes = scopes
        role.save(update_fields=["permissions", "scopes"])


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0001_initial"),
        ("accounts", "0001_initial"),
    ]

    operations = [migrations.RunPython(forward, backward)]
