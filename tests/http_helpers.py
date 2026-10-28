"""Explicit HTTP test setup; business scenarios use a real registered account."""
from uuid import uuid4


def bootstrap(client, *, account=True):
    response = client.get("/api/bootstrap")
    assert response.status_code == 200, response.text
    client.headers["X-Travel-Token"] = response.json()["csrf_token"]
    if account and not response.json().get("user"):
        registered = client.post("/api/auth/signup", json={
            "email": f"http-{uuid4().hex}@example.com", "password": "HTTP test password 2026!"})
        assert registered.status_code == 201, registered.text
        client.headers["X-Travel-Token"] = registered.json()["csrf_token"]
        response = client.get("/api/bootstrap")
        assert response.status_code == 200 and response.json()["user"]
    return response
