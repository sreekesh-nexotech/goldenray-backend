from functools import lru_cache

import factory
from django.contrib.auth.hashers import make_password

from accounts.models import Role, User

DEFAULT_PASSWORD = "Correct-Horse-Battery-9"
NEW_PASSWORD = "Another-Staple-Lantern-42"


@lru_cache(maxsize=1)
def default_password_hash() -> str:
    """Argon2 hash of DEFAULT_PASSWORD, computed once per test session (hashing is deliberately slow)."""
    return make_password(DEFAULT_PASSWORD)


class RoleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Role

    slug = factory.Sequence(lambda n: f"role-{n}")
    name = factory.Sequence(lambda n: f"Role {n}")
    permissions = factory.LazyFunction(dict)
    scopes = factory.LazyFunction(dict)


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    role = factory.SubFactory(RoleFactory)
    password = factory.LazyFunction(default_password_hash)
    is_active = True


def super_admin_role() -> Role:
    """The seeded Super Admin role (seeding is idempotent)."""
    from accounts.services.authz import SUPER_ADMIN_SLUG
    from accounts.services.seeds import seed_roles

    seed_roles()
    return Role.objects.get(slug=SUPER_ADMIN_SLUG)


def seeded_role(slug: str) -> Role:
    from accounts.services.seeds import seed_roles

    seed_roles()
    return Role.objects.get(slug=slug)
