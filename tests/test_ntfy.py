"""Tests for orchestrator/ntfy.py — push notifications."""

from __future__ import annotations

from orchestrator import ntfy


def test_is_configured_false_when_topic_unset(no_ntfy_topic):
    assert ntfy.is_configured() is False


def test_is_configured_true_when_topic_set(with_ntfy_topic):
    assert ntfy.is_configured() is True


def test_push_returns_false_when_no_topic(no_ntfy_topic, capsys):
    assert ntfy.push("title", "body") is False
    captured = capsys.readouterr()
    assert "ntfy disabled" in captured.err
    assert "title" in captured.err


def test_push_returns_false_on_blank_topic(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "   ")
    assert ntfy.push("t", "b") is False


def test_push_hits_correct_url_with_headers(monkeypatch, with_ntfy_topic):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, content=None, headers=None):
            calls.append({"url": url, "content": content, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("orchestrator.ntfy.httpx.Client", FakeClient)

    assert ntfy.push("the title", "the body", priority="high", tags=["rotating_light"]) is True
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/test-topic-do-not-use")
    assert calls[0]["content"] == "the body"
    assert calls[0]["headers"]["Title"] == "the title"
    assert calls[0]["headers"]["Priority"] == "high"
    assert calls[0]["headers"]["Tags"] == "rotating_light"


def test_push_returns_false_on_exception(monkeypatch, with_ntfy_topic, capsys):
    class BlowUpClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, *args, **kwargs):
            raise ConnectionError("dns failed")

    monkeypatch.setattr("orchestrator.ntfy.httpx.Client", BlowUpClient)
    assert ntfy.push("t", "b") is False
    captured = capsys.readouterr()
    assert "ntfy push failed" in captured.err


def test_push_escalation_includes_pr_url(monkeypatch, with_ntfy_topic):
    captured_headers = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, content=None, headers=None):
            captured_headers.update(headers or {})
            return FakeResponse()

    monkeypatch.setattr("orchestrator.ntfy.httpx.Client", FakeClient)
    ntfy.push_escalation("F-001-U-1", "cap-3 hit", pr_url="https://github.com/o/r/pull/5")
    assert "F-001-U-1" in captured_headers["Title"]
    assert captured_headers["Priority"] == "high"
    assert "rotating_light" in captured_headers["Tags"]
    assert captured_headers["Click"] == "https://github.com/o/r/pull/5"


def test_push_ready_to_merge_uses_check_mark(monkeypatch, with_ntfy_topic):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, content=None, headers=None):
            captured.update(headers)
            captured["body"] = content
            return FakeResponse()

    monkeypatch.setattr("orchestrator.ntfy.httpx.Client", FakeClient)
    ntfy.push_ready_to_merge("F-001-U-2", "https://github.com/o/r/pull/9", summary="all green")
    assert "white_check_mark" in captured["Tags"]
    assert captured["Priority"] == "default"
    assert "all green" in captured["body"]
