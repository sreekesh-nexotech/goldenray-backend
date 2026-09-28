# audit_log is a range-partitioned table (monthly on `at`), which Django cannot declare: the table, its DEFAULT
# partition and its indexes are created with raw SQL and the Django model is unmanaged (state only).

from datetime import date

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

CREATE_SQL = """
CREATE TABLE audit_log (
    id bigserial NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    actor_id bigint NULL,
    actor_kind varchar(8) NOT NULL DEFAULT 'SYSTEM',
    request_id uuid NULL,
    ip inet NULL,
    action varchar(64) NOT NULL,
    object_type varchar(64) NOT NULL DEFAULT '',
    object_uid uuid NULL,
    before jsonb NULL,
    after jsonb NULL,
    note text NOT NULL DEFAULT '',
    CONSTRAINT audit_log_pkey PRIMARY KEY (id, at),
    CONSTRAINT audit_log_actor_kind_valid CHECK (actor_kind IN ('USER', 'AGENT', 'DEVICE', 'CUSTOMER', 'SYSTEM')),
    CONSTRAINT audit_log_action_not_blank CHECK (action <> '')
    -- actor_id references accounts_user(id) WITHOUT a database constraint (DV-9): an ON DELETE action would rewrite
    -- ledger rows. Users are only ever soft-deleted, so the reference stays valid.
) PARTITION BY RANGE (at);

CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT;

-- Query-shaped indexes (PLAN §2.1): object history, actor history, action history — each ordered by time.
CREATE INDEX audit_log_object_idx ON audit_log (object_type, object_uid, at);
CREATE INDEX audit_log_actor_idx ON audit_log (actor_id, at);
CREATE INDEX audit_log_action_idx ON audit_log (action, at);
"""

DROP_SQL = "DROP TABLE IF EXISTS audit_log CASCADE;"


def _month_start(value, months):
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def create_initial_partitions(apps, schema_editor):
    """The current UTC month and the next two; `ensure_audit_partitions` keeps extending the window."""
    today = django.utils.timezone.now().date()
    quote = schema_editor.connection.ops.quote_name
    for offset in range(3):
        start, end = _month_start(today, offset), _month_start(today, offset + 1)
        name = f"audit_log_y{start.year:04d}m{start.month:02d}"
        schema_editor.execute(
            f"CREATE TABLE IF NOT EXISTS {quote(name)} PARTITION OF audit_log "
            f"FOR VALUES FROM ('{start.isoformat()} 00:00:00+00') TO ('{end.isoformat()} 00:00:00+00')"
        )


def apply_append_only(apps, schema_editor):
    """REVOKE UPDATE, DELETE, TRUNCATE from DB_APP_ROLE when the app connects as a role other than the owner."""
    role = (getattr(settings, "DB_APP_ROLE", "") or "").strip()
    if not role:
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] == role:
            return
    from audit.services.partitions import apply_append_only_privileges

    apply_append_only_privileges(role)


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("accounts", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL(CREATE_SQL, DROP_SQL),
        migrations.RunPython(create_initial_partitions, migrations.RunPython.noop),
        migrations.RunPython(apply_append_only, migrations.RunPython.noop),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("pk", models.CompositePrimaryKey("id", "at", blank=True, editable=False, primary_key=True, serialize=False)),
                (
                    "id",
                    models.BigIntegerField(
                        db_default=models.Func(models.Value("audit_log_id_seq"), function="nextval", output_field=models.BigIntegerField()), editable=False
                    ),
                ),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "actor_kind",
                    models.CharField(
                        choices=[("USER", "User"), ("AGENT", "Office agent"), ("DEVICE", "Device"), ("CUSTOMER", "Customer"), ("SYSTEM", "System")],
                        default="SYSTEM",
                        max_length=8,
                    ),
                ),
                ("request_id", models.UUIDField(blank=True, null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("action", models.CharField(max_length=64)),
                ("object_type", models.CharField(blank=True, default="", max_length=64)),
                ("object_uid", models.UUIDField(blank=True, null=True)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("note", models.TextField(blank=True, default="")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "db_table": "audit_log",
                "managed": False,
                "default_permissions": (),
            },
        ),
    ]

