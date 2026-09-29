import pytest


@pytest.fixture
def procurement_user(make_user):
    return make_user(grants={"procurement": "*", "pricing_internal": ["view"], "catalog": ["view"]})


@pytest.fixture
def client(auth_client, procurement_user):
    return auth_client(procurement_user)


@pytest.fixture
def clerk(auth_client, make_user):
    """procurement view/create/edit, no commit, no pricing_internal."""
    return auth_client(make_user(grants={"procurement": ["view", "create", "edit"]}))


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"procurement": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    return auth_client(make_user(grants={"pricing": ["view"]}))
