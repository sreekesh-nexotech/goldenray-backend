import pytest

from emi.services import pack_release


@pytest.fixture(autouse=True)
def _restore_the_pack_release_provider():
    """Tests replace or remove the ``PACK_RELEASE`` provider; put back the one ``EmiConfig.ready`` installed."""
    yield
    pack_release.install()
