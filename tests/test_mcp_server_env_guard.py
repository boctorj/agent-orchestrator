"""F-016-U-7 credential hardening — MCP server boot refuses bad ANTHROPIC_API_KEY."""

from __future__ import annotations

import pytest

from orchestrator import mcp_server


def test_mcp_server_main_refuses_bad_anthropic_key(monkeypatch, capsys):
    """The MCP server's ``main()`` refuses to call ``mcp.run()`` when
    ``ANTHROPIC_API_KEY`` doesn't pass the shape check. Closes the
    F-016-U-7 foot-gun where a stale shell-rc export shadows ``.env``
    and every ``spawn_unit`` call returns an opaque Anthropic 401."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "lkj")
    # Belt-and-suspenders: defang init_db so a misfire wouldn't touch
    # the real state.db.
    monkeypatch.setattr("orchestrator.state.init_db", lambda: None)

    with pytest.raises(SystemExit) as exc:
        mcp_server.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "refusing to start" in captured.err


def test_mcp_server_main_accepts_valid_key(monkeypatch):
    """A valid ``sk-ant-`` key passes the guard and proceeds to ``mcp.run()``."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    monkeypatch.setattr("orchestrator.state.init_db", lambda: None)

    ran: list[bool] = []
    # Patch ``mcp.run`` on the FastMCP instance imported by mcp_server.
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: ran.append(True))

    mcp_server.main()
    assert ran == [True]
