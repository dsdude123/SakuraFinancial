import pytest
from fastapi.testclient import TestClient

from settings_service.main import create_app


@pytest.fixture()
def client():
    app = create_app(database_url="sqlite://")
    with TestClient(app) as test_client:
        yield test_client
