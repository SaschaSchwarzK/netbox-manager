from fastapi.testclient import TestClient

from app.main import app


def test_api_docs_are_disabled_by_default():
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
