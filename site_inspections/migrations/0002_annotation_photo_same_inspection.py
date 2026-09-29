"""Composite foreign key ``(photo_id, inspection_id) → site_inspections_photo(id, inspection_id)`` (PLAN §2.7).

An annotation can only reference a photo of its own inspection; the service checks it too, the database makes it
impossible. The target unique constraint ``site_inspections_photo_id_inspection_uniq`` is created by 0001.
"""

from django.db import migrations

FK = "site_inspections_annotation_photo_same_inspection_fk"


class Migration(migrations.Migration):
    dependencies = [("site_inspections", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE site_inspections_annotation ADD CONSTRAINT {FK} FOREIGN KEY (photo_id, inspection_id) "
                "REFERENCES site_inspections_photo (id, inspection_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;"
            ),
            reverse_sql=f"ALTER TABLE site_inspections_annotation DROP CONSTRAINT {FK};",
        )
    ]
