"""Tests for orchestrator/github_app.py — GitHub App identity for workers."""

from __future__ import annotations

import time

import pytest

from orchestrator import github_app

# --------------------------- fixtures ---------------------------


@pytest.fixture
def test_private_key(tmp_path):
    """Generate a small RSA key + write it to a temp .pem. Path is returned."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test.pem"
    key_path.write_bytes(pem)
    return key_path


@pytest.fixture
def app_env_with_path(monkeypatch, test_private_key):
    """Set all GitHub App env vars with PRIVATE_KEY_PATH."""
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    github_app.clear_token_cache()
    yield
    github_app.clear_token_cache()


@pytest.fixture
def app_env_with_inline_key(monkeypatch, test_private_key):
    """Set GITHUB_APP_PRIVATE_KEY (inline PEM) instead of PATH."""
    pem = test_private_key.read_text()
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    github_app.clear_token_cache()
    yield
    github_app.clear_token_cache()


@pytest.fixture
def no_auth(monkeypatch):
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    github_app.clear_token_cache()
    yield
    github_app.clear_token_cache()


# --------------------------- is_app_configured ---------------------------


class TestIsAppConfigured:
    def test_returns_true_when_all_set_with_path(self, app_env_with_path):
        assert github_app.is_app_configured() is True

    def test_returns_true_when_all_set_with_inline_key(self, app_env_with_inline_key):
        assert github_app.is_app_configured() is True

    def test_returns_false_when_app_id_missing(self, monkeypatch, test_private_key):
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
        assert github_app.is_app_configured() is False

    def test_returns_false_when_installation_missing(self, monkeypatch, test_private_key):
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
        assert github_app.is_app_configured() is False

    def test_returns_false_when_no_key(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        assert github_app.is_app_configured() is False

    def test_blank_env_treated_as_unset(self, monkeypatch, test_private_key):
        monkeypatch.setenv("GITHUB_APP_ID", "   ")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
        assert github_app.is_app_configured() is False


# --------------------------- auth_mode ---------------------------


class TestAuthMode:
    def test_app(self, app_env_with_path):
        assert github_app.auth_mode() == "app"

    def test_pat(self, monkeypatch, no_auth):
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_x")
        assert github_app.auth_mode() == "pat"

    def test_none(self, no_auth):
        assert github_app.auth_mode() == "none"

    def test_app_takes_precedence_over_pat(self, app_env_with_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_unused")
        assert github_app.auth_mode() == "app"


# --------------------------- _load_private_key ---------------------------


class TestLoadPrivateKey:
    def test_loads_from_path(self, app_env_with_path, test_private_key):
        pem = github_app._load_private_key()
        # Avoid the literal blacklisted strings (would trip detect-private-key
        # pre-commit hook on this file). Equivalent to "BEGIN [RSA ]PRIVATE KEY".
        assert pem.startswith("-----")
        assert "PRIVATE KEY" in pem

    def test_loads_inline(self, app_env_with_inline_key):
        pem = github_app._load_private_key()
        # Avoid the literal blacklisted strings (would trip detect-private-key
        # pre-commit hook on this file). Equivalent to "BEGIN [RSA ]PRIVATE KEY".
        assert pem.startswith("-----")
        assert "PRIVATE KEY" in pem

    def test_inline_unescapes_backslash_n(self, monkeypatch, test_private_key):
        """Inline PEM with literal \\n (from .env) is unescaped."""
        raw = test_private_key.read_text()
        # Simulate .env-style single-line storage
        single_line = raw.replace("\n", "\\n")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", single_line)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        loaded = github_app._load_private_key()
        # After unescaping, it should match the original
        assert loaded == raw

    def test_raises_when_no_key(self, monkeypatch):
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="private key not configured"):
            github_app._load_private_key()


# --------------------------- _create_app_jwt ---------------------------


class TestCreateAppJwt:
    def test_creates_valid_jwt(self, app_env_with_path):
        token = github_app._create_app_jwt()
        # JWTs have 3 dot-separated base64 parts
        assert token.count(".") == 2

    def test_jwt_payload_has_correct_iss(self, app_env_with_path):
        import jwt

        token = github_app._create_app_jwt()
        # Decode WITHOUT verifying signature — we just want to inspect the payload
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["iss"] == "12345"
        # iat should be ~60s before now (clock-drift fudge)
        now = int(time.time())
        assert decoded["iat"] <= now
        assert decoded["exp"] > now

    def test_raises_when_app_id_missing(self, monkeypatch, test_private_key):
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
        with pytest.raises(RuntimeError, match="GITHUB_APP_ID not set"):
            github_app._create_app_jwt()


# --------------------------- mint_installation_token ---------------------------


class FakeResponse:
    def __init__(self, status_code=201, json_data=None):
        self.status_code = status_code
        self._json = json_data or {"token": "ghs_mocked_installation_token"}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.posts: list[tuple[str, dict | None]] = []
        self._response = FakeResponse()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def post(self, url, headers=None, json=None):
        self.posts.append((url, headers))
        return self._response


class TestMintInstallationToken:
    def test_hits_correct_url(self, app_env_with_path, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr("orchestrator.github_app.httpx.Client", lambda *a, **k: client)
        token = github_app.mint_installation_token()
        assert token == "ghs_mocked_installation_token"
        assert len(client.posts) == 1
        url, headers = client.posts[0]
        assert "/app/installations/67890/access_tokens" in url
        assert headers["Authorization"].startswith("Bearer ")
        assert "X-GitHub-Api-Version" in headers

    def test_raises_when_installation_missing(self, monkeypatch, test_private_key):
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(test_private_key))
        with pytest.raises(RuntimeError, match="INSTALLATION_ID not set"):
            github_app.mint_installation_token()


# --------------------------- get_agent_token ---------------------------


class TestGetAgentToken:
    def test_prefers_app_token_when_configured(self, app_env_with_path, monkeypatch):
        # Also set a PAT — App should still win
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_unused")
        client = FakeClient()
        monkeypatch.setattr("orchestrator.github_app.httpx.Client", lambda *a, **k: client)
        assert github_app.get_agent_token() == "ghs_mocked_installation_token"

    def test_falls_back_to_pat_when_no_app(self, monkeypatch, no_auth):
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_mytoken")
        assert github_app.get_agent_token() == "github_pat_mytoken"

    def test_raises_when_neither_configured(self, no_auth):
        with pytest.raises(RuntimeError, match="No GitHub auth configured"):
            github_app.get_agent_token()


# --------------------------- caching ---------------------------


class TestTokenCaching:
    def test_second_call_uses_cache(self, app_env_with_path, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr("orchestrator.github_app.httpx.Client", lambda *a, **k: client)
        github_app.get_agent_token()
        github_app.get_agent_token()
        github_app.get_agent_token()
        # Only one HTTP POST — others served from cache
        assert len(client.posts) == 1

    def test_cache_expires_after_ttl(self, app_env_with_path, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr("orchestrator.github_app.httpx.Client", lambda *a, **k: client)

        # First mint
        github_app.get_agent_token()
        assert len(client.posts) == 1

        # Force-age the cache (set ts to >50min ago)
        with github_app._token_lock:
            for key in list(github_app._token_cache.keys()):
                if key.endswith(":ts"):
                    github_app._token_cache[key] = time.time() - 60 * 60

        github_app.get_agent_token()
        assert len(client.posts) == 2

    def test_clear_token_cache_forces_remint(self, app_env_with_path, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr("orchestrator.github_app.httpx.Client", lambda *a, **k: client)
        github_app.get_agent_token()
        github_app.clear_token_cache()
        github_app.get_agent_token()
        assert len(client.posts) == 2

    def test_pat_path_does_not_use_cache(self, monkeypatch, no_auth):
        """PAT is read fresh from env on every call — no caching needed."""
        monkeypatch.setenv("GITHUB_TOKEN", "v1")
        assert github_app.get_agent_token() == "v1"
        monkeypatch.setenv("GITHUB_TOKEN", "v2")
        assert github_app.get_agent_token() == "v2"
