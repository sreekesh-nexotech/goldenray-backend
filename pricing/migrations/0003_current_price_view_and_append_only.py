# The ``pricing_current_price`` VIEW (mapped by the unmanaged ``pricing.CurrentPrice``) and the database guard that makes
# ``pricing_price`` append-only: every DELETE is refused, and an UPDATE may only close an open row (``effective_to``
# NULL → a date), stamp ``updated_at``/``updated_by``/``version``, or null an attribution FK (user/supplier deletion).

from django.db import migrations

VIEW_SQL = """
CREATE VIEW pricing_current_price AS
SELECT DISTINCT ON (p.component_id, p.kind)
       p.id, p.uid, p.component_id, p.kind, p.amount, p.currency, p.gst_inclusive, p.per_watt, p.effective_from,
       p.source, p.source_ref, p.supplier_id, p.note, p.version_key, p.created_at
  FROM pricing_price p
 WHERE p.effective_to IS NULL AND p.deleted_at IS NULL
 ORDER BY p.component_id, p.kind, p.effective_from DESC, p.id DESC;
"""

GUARD_SQL = """
CREATE FUNCTION pricing_price_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pricing_price is append-only: rows are never deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN
        RAISE EXCEPTION 'pricing_price: a closed price row stays closed' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.uid, NEW.component_id, NEW.kind, NEW.amount, NEW.currency, NEW.gst_inclusive, NEW.per_watt,
        NEW.effective_from, NEW.source, NEW.source_ref, NEW.note, NEW.version_key, NEW.created_at, NEW.deleted_at)
       IS DISTINCT FROM
       (OLD.id, OLD.uid, OLD.component_id, OLD.kind, OLD.amount, OLD.currency, OLD.gst_inclusive, OLD.per_watt,
        OLD.effective_from, OLD.source, OLD.source_ref, OLD.note, OLD.version_key, OLD.created_at, OLD.deleted_at) THEN
        RAISE EXCEPTION 'pricing_price is append-only: only effective_to may change' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.supplier_id IS DISTINCT FROM OLD.supplier_id AND NEW.supplier_id IS NOT NULL THEN
        RAISE EXCEPTION 'pricing_price is append-only: the supplier cannot change' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NEW.created_by_id IS NOT NULL THEN
        RAISE EXCEPTION 'pricing_price is append-only: the creator cannot change' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER pricing_price_append_only BEFORE UPDATE OR DELETE ON pricing_price
    FOR EACH ROW EXECUTE FUNCTION pricing_price_append_only();
"""

DROP_GUARD_SQL = """
DROP TRIGGER IF EXISTS pricing_price_append_only ON pricing_price;
DROP FUNCTION IF EXISTS pricing_price_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("pricing", "0002_relations"),
    ]

    operations = [
        migrations.RunSQL(VIEW_SQL, "DROP VIEW IF EXISTS pricing_current_price;"),
        migrations.RunSQL(GUARD_SQL, DROP_GUARD_SQL),
    ]
