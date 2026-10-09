from app.routers.device_types import _with_actor_trailer


def test_pr_trailer_includes_oidc_username_when_available():
    body = _with_actor_trailer("Change details", {
        "sub": "123", "name": "Alice Example", "email": "alice@example.com",
        "username": "alice",
    })

    assert body == (
        "Change details\n\n---\n"
        "Requested via NetBox Manager by: Alice Example <alice@example.com>\n"
        "OIDC username: alice"
    )


def test_pr_trailer_omits_username_line_when_claim_is_unavailable():
    body = _with_actor_trailer("Change details", {
        "sub": "123", "name": "Alice Example", "email": "alice@example.com",
        "username": None,
    })

    assert "OIDC username:" not in body
