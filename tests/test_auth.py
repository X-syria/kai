import httpx


def _register(client: httpx.AsyncClient, email: str, password: str) -> dict:
    return httpx.post(
        client.base_url + "/api/v1/auth/register",
        json={"email": email, "password": password},
    ).json()
