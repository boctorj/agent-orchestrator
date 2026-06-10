"""Tests for orchestrator/env_guard.py — credential-hardening helpers (F-016-U-7)."""

from __future__ import annotations

import pytest

from orchestrator import env_guard

# --------------------------- read_env_file_values ---------------------------


def test_read_env_file_values_missing(tmp_path):
    assert env_guard.read_env_file_values(tmp_path / "no-such.env") == {}


def test_read_env_file_values_parses_keys(tmp_path):
    f = tmp_path / ".env"
    f.write_text("ANTHROPIC_API_KEY=sk-ant-real\nGITHUB_TOKEN=github_pat_abc\nNTFY_TOPIC=mytopic\n")
    assert env_guard.read_env_file_values(f) == {
        "ANTHROPIC_API_KEY": "sk-ant-real",
        "GITHUB_TOKEN": "github_pat_abc",
        "NTFY_TOPIC": "mytopic",
    }


def test_read_env_file_values_skips_comments_and_blanks(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# top comment\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-ant-real\n"
        "# inline comment line\n"
        "  \n"
        "GITHUB_TOKEN=github_pat_abc\n"
    )
    out = env_guard.read_env_file_values(f)
    assert "ANTHROPIC_API_KEY" in out
    assert "GITHUB_TOKEN" in out


def test_read_env_file_values_strips_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text("ANTHROPIC_API_KEY=\"sk-ant-quoted\"\nGITHUB_TOKEN='github_pat_q'\n")
    out = env_guard.read_env_file_values(f)
    assert out["ANTHROPIC_API_KEY"] == "sk-ant-quoted"
    assert out["GITHUB_TOKEN"] == "github_pat_q"


def test_read_env_file_values_handles_empty_value(tmp_path):
    """Blank values (``NTFY_TOPIC=``) must round-trip as empty strings,
    not appear as missing keys — the orchestrator init template emits
    this exact shape for the optional ntfy topic."""
    f = tmp_path / ".env"
    f.write_text("NTFY_TOPIC=\n")
    assert env_guard.read_env_file_values(f) == {"NTFY_TOPIC": ""}


# --------------------------- is_valid_anthropic_key ---------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("sk-ant-abcdef", True),
        ("sk-ant-", True),
        ("", False),
        ("not-a-key", False),
        ("sk-fake-but-wrong-prefix", False),
        ("SK-ANT-uppercase", False),  # case-sensitive
    ],
)
def test_is_valid_anthropic_key(value, expected):
    assert env_guard.is_valid_anthropic_key(value) is expected


# --------------------------- anthropic_key_diagnostic ---------------------------


def test_diagnostic_mentions_shell_rc_when_env_file_has_valid_key():
    """The F-016-U-7 spec foot-gun: ``.env`` has a real key but the
    shell-rc export shadows it. The diagnostic must name the rc files."""
    out = env_guard.anthropic_key_diagnostic(
        resolved_value="lkj",
        env_file_value="sk-ant-real-from-env-file",
    )
    assert ".env has a valid-looking key" in out
    assert "~/.zshrc" in out
    assert ".bashrc" in out


def test_diagnostic_redacts_resolved_prefix():
    out = env_guard.anthropic_key_diagnostic(resolved_value="lkjpoiqwer123456")
    # First 8 chars + ellipsis — operator can correlate with the shell
    # export without us leaking the whole credential.
    assert "lkjpoiqw" in out


def test_diagnostic_no_env_file_value_omits_shadow_language():
    """If ``.env`` doesn't have a valid key either, the diagnostic
    should NOT claim the .env is being shadowed — the user needs to
    fix the .env value, not unset a shell export."""
    out = env_guard.anthropic_key_diagnostic(resolved_value="lkj", env_file_value="")
    assert "shadowed" not in out
    assert "sk-ant-" in out


# --------------------------- detect_env_shadowing ---------------------------


def test_shadowing_detected_when_values_differ():
    findings = env_guard.detect_env_shadowing(
        process_env={"ANTHROPIC_API_KEY": "lkj"},
        env_file_values={"ANTHROPIC_API_KEY": "sk-ant-real"},
    )
    assert len(findings) == 1
    assert findings[0]["name"] == "ANTHROPIC_API_KEY"


def test_no_shadowing_when_values_match():
    findings = env_guard.detect_env_shadowing(
        process_env={"ANTHROPIC_API_KEY": "sk-ant-real"},
        env_file_values={"ANTHROPIC_API_KEY": "sk-ant-real"},
    )
    assert findings == []


def test_no_shadowing_when_only_one_side_set():
    """``.env`` has it but the shell doesn't — that's the normal case;
    not a shadow. And vice versa: a shell export with no ``.env`` row
    is the operator's intentional override, also not a shadow."""
    findings = env_guard.detect_env_shadowing(
        process_env={},
        env_file_values={"ANTHROPIC_API_KEY": "sk-ant-real"},
    )
    assert findings == []

    findings = env_guard.detect_env_shadowing(
        process_env={"ANTHROPIC_API_KEY": "sk-ant-real"},
        env_file_values={},
    )
    assert findings == []


def test_shadowing_only_checks_relevant_keys():
    """A divergence on an unrelated env var must NOT show up — the
    audit is scoped to orchestrator-relevant vars (else doctor would
    flag every PATH / HOME shell difference)."""
    findings = env_guard.detect_env_shadowing(
        process_env={"UNRELATED_VAR": "shell-value"},
        env_file_values={"UNRELATED_VAR": "env-file-value"},
    )
    assert findings == []


# --------------------------- format_shadowing_finding ---------------------------


def test_format_shadowing_redacts_both_values():
    out = env_guard.format_shadowing_finding(
        {
            "name": "ANTHROPIC_API_KEY",
            "env_value": "lkj123456789",
            "file_value": "sk-ant-realsecret",
        }
    )
    assert "shadowing detected" in out
    assert "ANTHROPIC_API_KEY" in out
    # Both sides redacted
    assert "lkj123456789" not in out
    assert "sk-ant-realsecret" not in out
