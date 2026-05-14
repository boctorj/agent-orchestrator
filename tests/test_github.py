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

    def put(self, url, **kwargs):
        return self._handle("PUT", url, **kwargs)


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


# --------------------------- submit_pr_review ---------------------------


class TestSubmitPrReview:
    def test_posts_to_reviews_endpoint_with_event_and_body(self, fake_httpx):
        github.submit_pr_review("https://github.com/o/r", 42, "all green now", event="COMMENT")
        client = fake_httpx[-1]
        assert len(client.calls) == 1
        method, url, payload = client.calls[0]
        assert method == "POST"
        assert url.endswith("/repos/o/r/pulls/42/reviews")
        assert payload == {"body": "all green now", "event": "COMMENT"}

    def test_event_defaults_to_comment(self, fake_httpx):
        github.submit_pr_review("https://github.com/o/r", 7, "endorse")
        _, _, payload = fake_httpx[-1].calls[0]
        assert payload["event"] == "COMMENT"

    def test_request_changes_event_passes_through(self, fake_httpx):
        github.submit_pr_review("https://github.com/o/r", 7, "fix this", event="REQUEST_CHANGES")
        _, _, payload = fake_httpx[-1].calls[0]
        assert payload["event"] == "REQUEST_CHANGES"

    def test_raises_on_http_error(self, fake_httpx):
        client = FakeClient()
        client.responder = lambda method, url, **kw: FakeResponse(422, text="invalid event")

        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            with pytest.raises(RuntimeError, match="HTTP 422"):
                gh.submit_pr_review("https://github.com/o/r", 1, "x")
        finally:
            gh.httpx.Client = orig


# --------------------------- dismiss_own_change_requests ---------------------------


class TestDismissOwnChangeRequests:
    def _build_client(self, my_login: str, reviews: list[dict]):
        """Stage a FakeClient that returns my_login on /user and reviews on the
        first reviews GET, then echoes 200 OK for every subsequent call."""
        client = FakeClient()
        call_idx = [0]

        def responder(method, url, **kwargs):
            call_idx[0] += 1
            if call_idx[0] == 1:  # GET /user
                return FakeResponse(200, {"login": my_login})
            if call_idx[0] == 2:  # GET .../reviews
                return FakeResponse(200, reviews)
            return FakeResponse(200, {})  # PUT .../dismissals

        client.responder = responder
        return client

    def _run_with(self, client, *args, **kwargs):
        import orchestrator.github as gh

        orig = gh.httpx.Client
        gh.httpx.Client = lambda *a, **k: client
        try:
            return gh.dismiss_own_change_requests(*args, **kwargs)
        finally:
            gh.httpx.Client = orig

    def test_dismiss_payload_has_only_message_no_event(self, fake_httpx):
        """Regression: a Copilot review on PR #17 flagged that passing
        `event: "DISMISS"` causes the GitHub dismissal endpoint to reject
        the request. The body must contain ONLY `message`."""
        client = self._build_client(
            my_login="orch-bot",
            reviews=[
                {"id": 100, "state": "CHANGES_REQUESTED", "user": {"login": "orch-bot"}},
            ],
        )
        count = self._run_with(client, "https://github.com/o/r", 1, "superseded")
        assert count == 1
        put_calls = [c for c in client.calls if c[0] == "PUT"]
        assert len(put_calls) == 1
        _, url, payload = put_calls[0]
        assert url.endswith("/repos/o/r/pulls/1/reviews/100/dismissals")
        assert payload == {"message": "superseded"}
        assert "event" not in payload

    def test_skips_other_users_reviews(self, with_github_token):
        client = self._build_client(
            my_login="orch-bot",
            reviews=[
                {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
                {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": "orch-bot"}},
            ],
        )
        count = self._run_with(client, "https://github.com/o/r", 1, "msg")
        assert count == 1
        put_urls = [c[1] for c in client.calls if c[0] == "PUT"]
        assert any("/reviews/2/" in u for u in put_urls)
        assert not any("/reviews/1/" in u for u in put_urls)

    def test_skips_non_changes_requested_reviews(self, with_github_token):
        client = self._build_client(
            my_login="orch-bot",
            reviews=[
                {"id": 1, "state": "APPROVED", "user": {"login": "orch-bot"}},
                {"id": 2, "state": "COMMENTED", "user": {"login": "orch-bot"}},
                {"id": 3, "state": "CHANGES_REQUESTED", "user": {"login": "orch-bot"}},
            ],
        )
        count = self._run_with(client, "https://github.com/o/r", 1, "msg")
        assert count == 1
        put_urls = [c[1] for c in client.calls if c[0] == "PUT"]
        assert len(put_urls) == 1
        assert "/reviews/3/" in put_urls[0]

    def test_returns_zero_when_login_unknown(self, with_github_token):
        """If /user has no `login` field, bail out without making any PUTs."""
        client = FakeClient()
        client.responder = lambda method, url, **kw: FakeResponse(200, {})
        count = self._run_with(client, "https://github.com/o/r", 1, "msg")
        assert count == 0
        assert not any(c[0] == "PUT" for c in client.calls)

    def test_case_insensitive_login_match(self, with_github_token):
        """GitHub returns logins with arbitrary case; comparison must normalize."""
        client = self._build_client(
            my_login="Orch-Bot",
            reviews=[
                {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": "orch-bot"}},
            ],
        )
        count = self._run_with(client, "https://github.com/o/r", 1, "msg")
        assert count == 1

    def test_swallows_per_review_http_error(self, with_github_token):
        """One failing dismissal must not break the loop for the rest."""
        client = FakeClient()
        call_idx = [0]

        def responder(method, url, **kwargs):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return FakeResponse(200, {"login": "bot"})
            if call_idx[0] == 2:
                return FakeResponse(
                    200,
                    [
                        {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": "bot"}},
                        {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": "bot"}},
                    ],
                )
            if "/reviews/1/" in url:
                import httpx as real_httpx

                raise real_httpx.HTTPError("transient")
            return FakeResponse(200, {})

        client.responder = responder
        count = self._run_with(client, "https://github.com/o/r", 1, "msg")
        assert count == 1  # only #2 dismissed
