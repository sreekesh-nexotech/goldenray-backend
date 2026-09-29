"""CSV import request/report shapes."""

from rest_framework import serializers


class ImportRequestSerializer(serializers.Serializer):
    file = serializers.FileField(use_url=False, help_text="UTF-8 CSV in the export format (≤ 2 MB, ≤ 5,000 rows).")
    dry_run = serializers.BooleanField(default=True, help_text="true: validate and simulate only (default); false: commit (needs import_token).")
    import_token = serializers.CharField(required=False, allow_blank=True, help_text="From a clean dry run of the same file (valid 1 hour).")


class ImportRowSerializer(serializers.Serializer):
    line = serializers.IntegerField(help_text="Line number in the file (the header is line 1).")
    sku = serializers.CharField(allow_blank=True)
    action = serializers.ChoiceField(choices=["create", "update", "unchanged", "error"])
    errors = serializers.DictField(child=serializers.JSONField())
    brand_created = serializers.CharField(allow_blank=True, help_text="A brand this row creates (blank: none).")


class ImportSummarySerializer(serializers.Serializer):
    rows = serializers.IntegerField()
    create = serializers.IntegerField()
    update = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    error = serializers.IntegerField()


class ImportReportSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField()
    committed = serializers.BooleanField()
    import_token = serializers.CharField(allow_null=True, help_text="Send with dry_run=false to commit this file.")
    summary = ImportSummarySerializer()
    rows = ImportRowSerializer(many=True)
