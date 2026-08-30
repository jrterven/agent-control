from __future__ import annotations

from .conftest import mutation_headers


PNG = b"\x89PNG\r\n\x1a\n" + (b"avatar" * 20)


def test_security_policy_allows_only_local_avatar_preview_blobs(authenticated):
    client, _ = authenticated
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "img-src 'self' data: blob:" in csp
    assert "img-src *" not in csp


def test_admin_can_assign_replace_and_remove_a_profile_avatar(authenticated):
    client, csrf = authenticated
    bootstrap = client.get("/api/v1/bootstrap").json()
    profile = bootstrap["profiles"][0]
    profile_id = profile["id"]
    assert profile["avatarUrl"] is None

    uploaded = client.put(
        f"/api/v1/profiles/{profile_id}/avatar",
        content=PNG,
        headers={
            **mutation_headers(csrf, "profile-avatar-upload"),
            "Content-Type": "image/png",
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    avatar_url = uploaded.json()["avatarUrl"]
    assert avatar_url.startswith(f"/api/v1/profiles/{profile_id}/avatar?v=")
    assert next(
        item for item in client.get("/api/v1/bootstrap").json()["profiles"]
        if item["id"] == profile_id
    )["avatarUrl"] == avatar_url

    image = client.get(avatar_url)
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["x-content-type-options"] == "nosniff"

    removed = client.delete(
        f"/api/v1/profiles/{profile_id}/avatar",
        headers=mutation_headers(csrf, "profile-avatar-delete"),
    )
    assert removed.status_code == 200
    assert removed.json() == {"avatarUrl": None}
    assert client.get(avatar_url).status_code == 404


def test_profile_avatar_rejects_unsupported_or_spoofed_content(authenticated):
    client, csrf = authenticated
    profile_id = client.get("/api/v1/bootstrap").json()["profiles"][0]["id"]

    unsupported = client.put(
        f"/api/v1/profiles/{profile_id}/avatar",
        content=b"not-an-image",
        headers={
            **mutation_headers(csrf, "profile-avatar-text"),
            "Content-Type": "text/plain",
        },
    )
    assert unsupported.status_code == 415

    spoofed = client.put(
        f"/api/v1/profiles/{profile_id}/avatar",
        content=b"not-a-real-png",
        headers={
            **mutation_headers(csrf, "profile-avatar-spoofed"),
            "Content-Type": "image/png",
        },
    )
    assert spoofed.status_code == 422
