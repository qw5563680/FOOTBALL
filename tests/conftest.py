import pytest
import responses


@pytest.fixture(autouse=True, scope="session")
def forbid_live_http():
    """An unregistered requests call must fail even outside a test's local mock."""
    with responses.RequestsMock():
        yield
