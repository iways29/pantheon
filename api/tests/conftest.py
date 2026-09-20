from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app

OWNER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
SECRET = "test-secret-long-enough-for-hs256-abcdefgh"
AUDIENCE = "authenticated"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        supabase_jwt_secret=SECRET,
        supabase_jwt_audience=AUDIENCE,
        owner_user_id=OWNER_ID,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()
