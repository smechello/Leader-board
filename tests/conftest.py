import os

import pytest
from dotenv import load_dotenv

load_dotenv()

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL is required for integration tests.", allow_module_level=True)

from app import app as flask_app  # noqa: E402


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_client(client, app):
    admin_username = app.config.get("ADMIN_USERNAME")
    if not admin_username:
        pytest.skip("ADMIN_USERNAME is required for admin integration tests.")

    with client.session_transaction() as session:
        session["_user_id"] = f"admin:{admin_username}"
        session["_fresh"] = True

    return client
