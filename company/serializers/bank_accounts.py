"""Bank account shapes. The full account number is write-only; responses carry the last four digits."""

from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from company.models import BankAccount
from core.serializers.common import ExpectedVersionMixin

ACCOUNT_NUMBER = RegexValidator(r"^[0-9]{9,18}$", "Use 9–18 digits.")
IFSC = RegexValidator(r"^[A-Z]{4}0[A-Z0-9]{6}$", "Not a valid IFSC (e.g. SBIN0001234).")
UPI = RegexValidator(r"^[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}$", "Not a valid UPI id (e.g. flarize@okaxis).")


class BankAccountSerializer(serializers.ModelSerializer):
    account_number_last4 = serializers.SerializerMethodField()
    account_number_masked = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = ["uid", "label", "bank", "account_name", "account_number_last4", "account_number_masked", "ifsc", "branch", "upi_id", "is_primary", "created_at", "updated_at", "version"]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_account_number_last4(self, account) -> str:
        return (account.account_number or "")[-4:]

    @extend_schema_field(serializers.CharField())
    def get_account_number_masked(self, account) -> str:
        number = account.account_number or ""
        return "X" * max(0, len(number) - 4) + number[-4:]


class _Upper(serializers.CharField):
    def to_internal_value(self, data):
        return super().to_internal_value(data).upper()


class _BankAccountWriteSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=80)
    bank = serializers.CharField(max_length=120)
    account_name = serializers.CharField(max_length=160)
    account_number = serializers.CharField(max_length=18, write_only=True, validators=[ACCOUNT_NUMBER])
    ifsc = _Upper(max_length=11, validators=[IFSC])
    branch = serializers.CharField(max_length=160, required=False, allow_blank=True)
    upi_id = serializers.CharField(max_length=100, required=False, allow_blank=True, validators=[UPI])

    def to_representation(self, instance):
        return BankAccountSerializer(instance, context=self.context).data


class BankAccountCreateSerializer(_BankAccountWriteSerializer):
    is_primary = serializers.BooleanField(required=False, default=False, help_text="The first account is always primary.")


class BankAccountUpdateSerializer(ExpectedVersionMixin, _BankAccountWriteSerializer):
    label = serializers.CharField(max_length=80, required=False)
    bank = serializers.CharField(max_length=120, required=False)
    account_name = serializers.CharField(max_length=160, required=False)
    account_number = serializers.CharField(max_length=18, write_only=True, required=False, validators=[ACCOUNT_NUMBER])
    ifsc = _Upper(max_length=11, required=False, validators=[IFSC])


class BankAccountActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass
