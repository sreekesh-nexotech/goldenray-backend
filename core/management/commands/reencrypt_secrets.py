"""``manage.py reencrypt_secrets [--dry-run]`` — Fernet key rotation (docs/ops/secrets.md).

Rotation: prepend the new key to ``FERNET_KEYS`` (``new,old``) and deploy; run this command, which re-encrypts every
``EncryptedTextField``/``EncryptedJSONField`` value of every model under the primary (first) key; then remove the old
key and deploy again. Values that no configured key can decrypt abort the run (fail closed) without changing
anything in that table.
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from flarize.crypto import DecryptionError, EncryptedJSONField, EncryptedTextField, reencrypt


def encrypted_columns():
    """``(model, [field, …])`` for every concrete model with encrypted fields."""
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        fields = [field for field in model._meta.concrete_fields if isinstance(field, (EncryptedTextField, EncryptedJSONField))]
        if fields:
            yield model, fields


class Command(BaseCommand):
    help = "Re-encrypt every Fernet-encrypted column under the first key of FERNET_KEYS."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count the values without writing.")

    def handle(self, *args, dry_run: bool, **options):
        total = 0
        quote = connection.ops.quote_name
        for model, fields in encrypted_columns():
            table, pk = model._meta.db_table, model._meta.pk.column
            with transaction.atomic():
                for field in fields:
                    column = field.column
                    with connection.cursor() as cursor:
                        cursor.execute(f"SELECT {quote(pk)}, {quote(column)} FROM {quote(table)} WHERE {quote(column)} IS NOT NULL FOR UPDATE")
                        rows = cursor.fetchall()
                        try:
                            rotated = [(reencrypt(value), key) for key, value in rows]
                        except DecryptionError as exc:
                            raise CommandError(f"{table}.{column}: a value cannot be decrypted with FERNET_KEYS; nothing in {table} was changed.") from exc
                        if not dry_run and rotated:
                            cursor.executemany(f"UPDATE {quote(table)} SET {quote(column)} = %s WHERE {quote(pk)} = %s", rotated)
                    total += len(rows)
                    self.stdout.write(f"{table}.{column}: {len(rows)} value(s){' (dry run)' if dry_run else ' re-encrypted'}")
        verb = "would be re-encrypted" if dry_run else "re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"{total} value(s) {verb} under the primary key"))
