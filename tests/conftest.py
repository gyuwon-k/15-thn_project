"""Test database lifecycle is independent of the user's application data."""
import pytest

@pytest.fixture(autouse=True)
def isolated_private_database():
    from backend import store
    store.shutdown()
    yield
    store.shutdown()
