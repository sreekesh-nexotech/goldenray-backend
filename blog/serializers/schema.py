"""Collections, templates, image groups and attribute slots (staff shapes). Validation of values lives in the services."""

from rest_framework import serializers

from blog.models import Collection, Template, TemplateAttributeSlot, TemplateImageGroup
from core.serializers.common import ExpectedVersionMixin


class CollectionSerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0, help_text="live entries in this collection")

    class Meta:
        model = Collection
        fields = ["uid", "api_uid", "singular_name", "plural_name", "description", "path_prefix", "is_active", "entry_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class CollectionWriteSerializer(serializers.Serializer):
    api_uid = serializers.CharField(max_length=80, help_text="public route segment (content/<api_uid>/)")
    singular_name = serializers.CharField(max_length=80)
    plural_name = serializers.CharField(max_length=80)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    path_prefix = serializers.CharField(max_length=120, help_text="site path entries are published under, e.g. /blog")
    is_active = serializers.BooleanField(required=False, default=True)

    def to_representation(self, instance):
        return CollectionSerializer(instance, context=self.context).data


class CollectionUpdateSerializer(ExpectedVersionMixin, CollectionWriteSerializer):
    api_uid = serializers.CharField(max_length=80, required=False)
    singular_name = serializers.CharField(max_length=80, required=False)
    plural_name = serializers.CharField(max_length=80, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    path_prefix = serializers.CharField(max_length=120, required=False)
    is_active = serializers.BooleanField(required=False)


class TemplateImageGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = TemplateImageGroup
        fields = ["uid", "key", "label", "repeatable", "max_items", "required", "position", "created_at", "updated_at", "version"]
        read_only_fields = fields


class TemplateAttributeSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = TemplateAttributeSlot
        fields = ["uid", "key", "label", "type", "options", "required", "position", "created_at", "updated_at", "version"]
        read_only_fields = fields


class TemplateSerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0)
    image_groups = TemplateImageGroupSerializer(many=True, read_only=True)
    attribute_slots = TemplateAttributeSlotSerializer(many=True, read_only=True)

    class Meta:
        model = Template
        fields = ["uid", "slug", "name", "description", "is_active", "sort_order", "entry_count", "image_groups", "attribute_slots", "created_at", "updated_at", "version"]
        read_only_fields = fields


class TemplateWriteSerializer(serializers.Serializer):
    slug = serializers.CharField(max_length=120)
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False, default=0)

    def to_representation(self, instance):
        return TemplateSerializer(instance, context=self.context).data


class TemplateUpdateSerializer(ExpectedVersionMixin, TemplateWriteSerializer):
    slug = serializers.CharField(max_length=120, required=False)
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False)


class TemplateDuplicateSerializer(serializers.Serializer):
    slug = serializers.CharField(max_length=120, help_text="slug of the copy")
    name = serializers.CharField(max_length=120, required=False, help_text="defaults to '<name> (copy)'")


class ImageGroupWriteSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=60, help_text="stable delivery key (imgUrls.<key>), immutable once created")
    label = serializers.CharField(max_length=120)
    repeatable = serializers.BooleanField(required=False, default=False)
    max_items = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    required = serializers.BooleanField(required=False, default=False)
    position = serializers.IntegerField(required=False, default=0)

    def to_representation(self, instance):
        return TemplateImageGroupSerializer(instance, context=self.context).data


class ImageGroupUpdateSerializer(ExpectedVersionMixin, ImageGroupWriteSerializer):
    key = serializers.CharField(max_length=60, required=False, help_text="immutable: a different value is refused")
    label = serializers.CharField(max_length=120, required=False)
    repeatable = serializers.BooleanField(required=False)
    max_items = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    required = serializers.BooleanField(required=False)
    position = serializers.IntegerField(required=False)


class AttributeSlotWriteSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=60, help_text="stable delivery key (attributes.<key>), immutable once created")
    label = serializers.CharField(max_length=120)
    type = serializers.ChoiceField(choices=TemplateAttributeSlot.Type.choices, required=False, default=TemplateAttributeSlot.Type.TEXT)
    options = serializers.JSONField(required=False, help_text='ENUM: {"choices": [...]}; NUMBER: {"min", "max", "default"}; others: {}')
    required = serializers.BooleanField(required=False, default=False)
    position = serializers.IntegerField(required=False, default=0)

    def to_representation(self, instance):
        return TemplateAttributeSlotSerializer(instance, context=self.context).data


class AttributeSlotUpdateSerializer(ExpectedVersionMixin, AttributeSlotWriteSerializer):
    key = serializers.CharField(max_length=60, required=False, help_text="immutable: a different value is refused")
    label = serializers.CharField(max_length=120, required=False)
    type = serializers.ChoiceField(choices=TemplateAttributeSlot.Type.choices, required=False)
    required = serializers.BooleanField(required=False)
    position = serializers.IntegerField(required=False)
