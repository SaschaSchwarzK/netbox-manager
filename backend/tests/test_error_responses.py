import asyncio
import json

from cryptography.fernet import InvalidToken
from starlette.requests import Request

from app.main import encrypted_credential_error, unexpected_api_error


def request(path="/api/test"):
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"", "server": ("test", 443), "client": ("test", 1), "scheme": "https"})


def body(response):
    return json.loads(response.body)["detail"]


def test_invalid_token_has_actionable_safe_response():
    response = asyncio.run(encrypted_credential_error(request(), InvalidToken()))
    detail = body(response)
    assert response.status_code == 503
    assert detail["code"] == "credential_decryption_failed"
    assert "NBM_SECRET_KEY" in detail["message"]
    assert "API token or GitHub PAT" in detail["message"]


def test_unexpected_error_has_request_reference_without_exception_text():
    response = asyncio.run(unexpected_api_error(request(), RuntimeError("sensitive detail")))
    detail = body(response)
    assert response.status_code == 500
    assert detail["code"] == "internal_error"
    assert "sensitive detail" not in detail["message"]
    assert "request_id" in detail
