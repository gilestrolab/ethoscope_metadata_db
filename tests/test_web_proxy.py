"""Running behind the SSO proxy: who may assert the user, and which origins may edit."""

import pytest
from fastapi.testclient import TestClient
from test_web import census_path  # noqa: F401 (fixture)

from ethoscope_metadata_db.web.app import Settings, create_app

PROXY = "192.0.2.10"  # RFC 5737 documentation addresses
USER = {"X-Forwarded-Email": "someone@lab"}


def client_from(path, address, **settings):
    app = create_app(Settings(path, trusted_proxies=(f"{PROXY}/32",), **settings))
    return TestClient(app, client=(address, 50000))


class TestTrustedProxy:
    def test_header_honoured_from_the_proxy(self, census_path):
        assert client_from(census_path, PROXY).get("/", headers=USER).status_code == 200

    def test_header_ignored_from_anywhere_else(self, census_path):
        # Reason: anyone on the campus network could otherwise claim to be any user.
        page = client_from(census_path, "203.0.113.99").get("/", headers=USER)
        assert page.status_code == 401 and "Not signed in" in page.text

    def test_unparseable_client_is_not_trusted(self, census_path):
        assert client_from(census_path, "testclient").get("/", headers=USER).status_code == 401

    def test_no_proxy_configured_trusts_any_client(self, census_path):
        assert (
            TestClient(create_app(Settings(census_path))).get("/", headers=USER).status_code == 200
        )

    def test_settings_from_env(self, monkeypatch, census_path):
        monkeypatch.setenv("ETHOCENSUS_DB", census_path)
        monkeypatch.setenv("ETHOCENSUS_TRUSTED_PROXIES", f" {PROXY}, 10.0.0.0/8 ")
        monkeypatch.setenv("ETHOCENSUS_PUBLIC_HOSTS", "metadata.lab.gilest.ro")
        settings = Settings.from_env()
        assert settings.trusted_proxies == (PROXY, "10.0.0.0/8")
        assert settings.public_hosts == ("metadata.lab.gilest.ro",)


class TestOriginBehindProxy:
    """The proxy may forward the upstream address as Host; the browser sends the public name."""

    @pytest.fixture
    def proxied(self, census_path):
        return client_from(census_path, PROXY, public_hosts=("metadata.lab.gilest.ro",))

    def post(self, client, origin, **headers):
        return client.post(
            "/notes",
            data={"target_type": "run", "target": "/x.db", "text": "hi", "back": "/"},
            headers={**USER, "Host": "198.51.100.5:8095", "Origin": origin, **headers},
            follow_redirects=False,
        )

    def test_public_host_may_edit(self, proxied):
        assert self.post(proxied, "https://metadata.lab.gilest.ro").status_code == 303

    def test_forwarded_host_may_edit(self, census_path):
        plain = client_from(census_path, PROXY)
        response = self.post(
            plain, "https://census.example", **{"X-Forwarded-Host": "census.example"}
        )
        assert response.status_code == 303

    def test_foreign_origin_is_refused(self, proxied):
        assert self.post(proxied, "https://evil.example").status_code == 403
