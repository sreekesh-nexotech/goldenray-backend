"""Authors, categories, tags and badges (staff shapes)."""

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from accounts.models import User
from blog.models import Author, Badge, Category, Tag
from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers.assets import MediaAssetRefSerializer

_COMMON = ["uid", "entry_count", "created_at", "updated_at", "version"]


class AuthorSerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0)
    avatar = MediaAssetRefSerializer(read_only=True, allow_null=True)
    user = serializers.SlugRelatedField(slug_field="uid", read_only=True, allow_null=True, help_text="the staff account writing as this author")

    class Meta:
        model = Author
        fields = ["uid", "name", "slug", "role", "bio", "avatar", "user", "entry_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class AuthorWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    slug = serializers.CharField(max_length=160, required=False, allow_blank=True, help_text="generated from the name when blank")
    role = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    bio = serializers.CharField(required=False, allow_blank=True, default="")
    avatar_uid = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), source="avatar", required=False, allow_null=True)
    user_uid = serializers.SlugRelatedField(slug_field="uid", queryset=User.objects.all(), source="user", required=False, allow_null=True)

    def to_representation(self, instance):
        return AuthorSerializer(instance, context=self.context).data


class AuthorUpdateSerializer(ExpectedVersionMixin, AuthorWriteSerializer):
    name = serializers.CharField(max_length=160, required=False)
    role = serializers.CharField(max_length=120, required=False, allow_blank=True)
    bio = serializers.CharField(required=False, allow_blank=True)


# The component names carry the app: catalog (product master) has its own Category* serializers in the same schema.
@extend_schema_serializer(component_name="BlogCategory")
class CategorySerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Category
        fields = ["uid", "name", "slug", *_COMMON[1:]]
        read_only_fields = fields


@extend_schema_serializer(component_name="BlogCategoryWrite")
class CategoryWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    slug = serializers.CharField(max_length=120, required=False, allow_blank=True, help_text="generated from the name when blank")

    def to_representation(self, instance):
        return CategorySerializer(instance, context=self.context).data


@extend_schema_serializer(component_name="BlogCategoryUpdate")
class CategoryUpdateSerializer(ExpectedVersionMixin, CategoryWriteSerializer):
    name = serializers.CharField(max_length=120, required=False)


class TagSerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Tag
        fields = ["uid", "name", "slug", *_COMMON[1:]]
        read_only_fields = fields


class TagWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    slug = serializers.CharField(max_length=80, required=False, allow_blank=True, help_text="generated from the name when blank")

    def to_representation(self, instance):
        return TagSerializer(instance, context=self.context).data


class TagUpdateSerializer(ExpectedVersionMixin, TagWriteSerializer):
    name = serializers.CharField(max_length=80, required=False)


class BadgeSerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Badge
        fields = ["uid", "name", "slug", "color", *_COMMON[1:]]
        read_only_fields = fields


class BadgeWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, help_text="delivered as `label`")
    slug = serializers.CharField(max_length=120, required=False, allow_blank=True, help_text="generated from the name when blank")
    color = serializers.CharField(max_length=9, required=False, default="#123532", help_text="hex colour, e.g. #ED8723")

    def to_representation(self, instance):
        return BadgeSerializer(instance, context=self.context).data


class BadgeUpdateSerializer(ExpectedVersionMixin, BadgeWriteSerializer):
    name = serializers.CharField(max_length=120, required=False)
    color = serializers.CharField(max_length=9, required=False)
