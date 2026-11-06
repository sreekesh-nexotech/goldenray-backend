"""``manage.py audit_change_request_roles`` — which roles can still write directly what change requests cover.

``approvals.0002`` only rewrites the system roles it knows. A custom role that already held direct catalog, pricing or
BOM edit rights keeps them, so its holders bypass Admin approval. This lists them (read-only; Admins and Super Admins,
who approve, are skipped) so each can be moved to ``change_requests.create`` in Studio → Roles, or left alone on purpose.
The grants checked come from the registered change kinds, so a kind added later is covered without editing this.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.models import Role
from approvals.services import kinds

APPROVERS = ("super-admin", "admin")


def direct_grants() -> dict[tuple[str, str], list[str]]:
    """``{(module, action): [kind labels]}`` — the direct grants that apply what a change request would."""
    grants: dict[tuple[str, str], list[str]] = {}
    for kind in kinds.registered():
        for action in kind.operations.values():
            grants.setdefault((kind.module, action), []).append(kind.label)
    return grants


def roles_bypassing_approval() -> list[dict]:
    grants = direct_grants()
    rows = []
    for role in Role.objects.filter(deleted_at__isnull=True).order_by("slug"):
        if role.slug in APPROVERS:
            continue
        permissions = role.permissions or {}
        held = sorted(f"{module}.{action}" for module, action in grants if action in permissions.get(module, []))
        if held:
            rows.append(
                {
                    "slug": role.slug,
                    "name": role.name,
                    "direct": held,
                    "can_request": "create" in permissions.get("change_requests", []),
                    "can_approve": "approve" in permissions.get("change_requests", []),
                }
            )
    return rows


class Command(BaseCommand):
    help = "List non-Admin roles that can still change catalog, pricing or BOM data directly (bypassing change requests)."

    def handle(self, *args, **options):
        rows = roles_bypassing_approval()
        if not rows:
            self.stdout.write(self.style.SUCCESS("No role other than Admin / Super Admin writes these directly."))
            return
        self.stdout.write(f"{len(rows)} role(s) can still write directly:")
        for row in rows:
            flags = ", ".join(flag for flag, on in (("can also request", row["can_request"]), ("can approve", row["can_approve"])) if on)
            self.stdout.write(f"  {row['slug']} ({row['name']}): {', '.join(row['direct'])}" + (f"  [{flags}]" if flags else ""))
        self.stdout.write("Move a role to change_requests.create (Studio → Roles) to put its changes through approval.")
