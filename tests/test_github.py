"""Tests for orchestrator/github.py — GitHub REST helpers.

Mocks `httpx.Client` to avoid real network calls. Verifies URL construction,
header injection, response parsing, and error handling.
"""

from __future__ import annotations

import pytest

from orchestrator import github

# --------------------------- parse_repo_url ---------------------------


class TestParseRepoUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/joe/repo", ("joe", "repo")),
            ("https://github.com/joe/repo/", ("joe", "repo")),
            ("https://github.com/joe/repo.git", ("joe", "repo")),
            ("http://github.com/owner/repo-with-dashes", ("owner", "repo-with-dashes")),
            ("https://github.com/org-name/repo_under.dot", ("org-name", "repo_under.dot")),
        ],
    )
    def test_valid_urls(self, url, expected):
        assert github.parse_repo_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "not-a-url",
            "https://gitlab.com/owner/repo",
            "https://github.com/no-repo",
            "",
        ],
    )
    def test_invalid_urls_raise(self, url):
        with pytest.raises(ValueError, match="Could not parse"):
            github.parse_repo_url(url)


# --------------------------- _headers ---------------------------


class TestHeaders:
    def test_raises_when_token_missing(self, no_github_token):
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN not set"):
            github._headers()

    def test_returns_bearer_when_token_set(self, with_github_token):
        h = github._headers()
        assert h["Authorization"] == "Bearer github_pat_fake_for_tests"
        assert h["Accept"] == "application/vnd.github+json"
        assert "X-GitHub-Api-Version" in h


# --------------------------- mocked-httpx helpers ---------------------------


class FakeResponse:
    def __init__(
        self, status_code: int = 200, json_data: dict | list | None = None, text: str = ""
    ):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class FakeClient:
    """Minimal httpx.Client stub that records calls and returns canned responses.

    Set `responses` as a list of (method, url_substring, response) tuples or
    pass a callable. Records all calls in `calls` for assertions.
    """

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.next_response: FakeResponse | None = None
        self.responder = None  # callable: (method, url, **kwargs) -> FakeResponse

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def _handle(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if self.responder:
            return self.responder(method, url, **kwargs)
        return self.next_response or FakeResponse()

    def get(self, url, **kwargs):
        return self._handle("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._handle("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._handle("PATCH", url, **kwargs)


@pytest.fixture
def fake_httpx(monkeypatch, with_github_token):
    """Replace httpx.Client constructor with FakeClient. Yields the client instance."""
    instances: list[FakeClient] = []

    def make_client(*args, **kwargs):
        c = FakeClient()
        instances.append(c)
        return c

    monkeypatch.setattr("orchestrator.github.httpx.Client", make_client)
    return instances


# --------------------------- amend_pr_body ---------------------------


class TestAmendPrBody:
    def test_fetches_then_patches_with_separator(self, fake_httpx):
        # GET returns existing body; PATCH should send new body
        def responder(method, url, **kwargs):
            if method == "GET":
                return FakeResponse(200, {"body": "original PR body"})
            return FakeResponse(200, {})

        github.amend_pr_body("https://github.com/o/r", 5, "appended line")
        client = fake_httpx[0]
        client.responder = responder
        # Re-run to capture both calls
        github.amend_pr_body("https://github.com/o/r", 5, "appended line")
        # The second client did GET + PATCH
        last_client = fake_httpx[-1]
        methods = [c[0] for c in last_client.calls]
        assert "GET" in methods
        assert "PATCH" in methods
        patch_call = next(c for c in last_client.calls if c[0] == "PATCH")
        new_body = patch_call[2]["body"]
        assert "appended line" in new_body
        assert "---" in new_body  # horizontal-rule separator

    def test_handles_null_body(self, fake_httpx):
        # GitHub returns "body": null for PRs without a description
        def responder(method, url, **kwargs):
            if method == "GET":
                return FakeResponse(200, {"body": None})
            return FakeResponse(200, {})

        github.amend_pr_body("https://github.com/o/r", 5, "first content")
        client = fake_httpx[-1]
        client.responder = responder
        github.amend_pr_body("https://github.com/o/r", 5, "first content")
        patch_call = next(c for c in fake_httpx[-1].calls if c[0] == "PATCH")
        # Should still produce a body without crashing on None
        assert patch_call[2]["body"].endswith("first content")


# --------------------------- post_pr_comment ---------------------------


class TestPostPrComment:
    def test_posts_to_issue_comments_endpoint(self, fake_httpx):
        github.post_pr_comment("https://github.com/o/r", 42, "hello")
        client = fake_httpx[-1]
        assert len(client.calls) == 1
        method, url, body = client.calls[0]
        assert method == "POST"
        assert "/issues/42/comments" in url
        assert body == {"body": "hello"}


# --------------------------- get_pr_state ---------------------------


class TestGetPrState:
    def test_extracts_relevant_fields(self, fake_httpx):
        sample = {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-11T19:00:00Z",
            "mergeable": None,
            "mergeable_state": "clean",
            "head": {"sha": "abc123"},
        }
        # Need a fresh FakeClient that returns this
        github.parse_repo_url("https://github.com/o/r")  # warm up no-op
        client = FakeClient()
        client.next_response = FakeResponse(200, sample)
        import orchestrator.github as gh

        # Inject directly
        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            result = gh.get_pr_state("https://github.com/o/r", 1)
        finally:
            gh.httpx.Client = orig
        assert result["state"] == "closed"
        assert result["merged"] is True
        assert result["head_sha"] == "abc123"
        assert result["merged_at"] == "2026-05-11T19:00:00Z"


# --------------------------- request_copilot_review ---------------------------


class TestRequestCopilotReview:
    def test_201_means_requested(self, fake_httpx):
        fake_httpx_inst = FakeClient()
        fake_httpx_inst.next_response = FakeResponse(201, {})
        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: fake_httpx_inst
        try:
            result = gh.request_copilot_review("https://github.com/o/r", 1)
        finally:
            gh.httpx.Client = orig
        assert result["requested"] is True
        assert result["status_code"] == 201

    def test_422_treated_as_already_requested(self, fake_httpx):
        client = FakeClient()
        client.next_response = FakeResponse(422, {})
        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            result = gh.request_copilot_review("https://github.com/o/r", 1)
        finally:
            gh.httpx.Client = orig
        assert result["requested"] is False
        assert result["status_code"] == 422
        assert "already" in result["note"].lower()

    def test_exception_returned_as_dict_not_raised(self, fake_httpx, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("network unreachable")

        monkeypatch.setattr("orchestrator.github.httpx.Client", boom)
        result = github.request_copilot_review("https://github.com/o/r", 1)
        assert result["requested"] is False
        assert "error" in result["note"].lower()


# --------------------------- get_copilot_review ---------------------------


class TestGetCopilotReview:
    def test_returns_none_when_no_copilot_review(self, fake_httpx):
        client = FakeClient()
        client.next_response = FakeResponse(
            200,
            [
                {"user": {"login": "joeboctor"}, "state": "COMMENTED", "body": "ours"},
            ],
        )
        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            result = gh.get_copilot_review("https://github.com/o/r", 1)
        finally:
            gh.httpx.Client = orig
        assert result is None

    def test_finds_copilot_and_extracts_inline_comments(self, fake_httpx):
        reviews_payload = [
            {
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
                "state": "COMMENTED",
                "body": "looks ok",
                "submitted_at": "2026-05-11T20:00:00Z",
            }
        ]
        comments_payload = [
            {"user": {"login": "Copilot"}, "path": "math.py", "line": 12, "body": "use enumerate"},
            {"user": {"login": "joeboctor"}, "path": "math.py", "line": 5, "body": "looks good"},
        ]

        call_idx = [0]
        client = FakeClient()

        def responder(method, url, **kwargs):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return FakeResponse(200, reviews_payload)
            return FakeResponse(200, comments_payload)

        client.responder = responder
        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            result = gh.get_copilot_review("https://github.com/o/r", 1)
        finally:
            gh.httpx.Client = orig

        assert result is not None
        assert result["author"] == "copilot-pull-request-reviewer[bot]"
        assert result["state"] == "COMMENTED"
        assert result["inline_count"] == 1
        # Only Copilot-authored comment is included
        assert result["inline_comments"][0]["body"] == "use enumerate"
        assert result["inline_comments"][0]["line"] == 12
