"""ntfy.sh push notifications for "your attention needed" events.

Free, no-account push notification service. User sets NTFY_TOPIC to a
hard-to-guess string in .env, subscribes via the ntfy mobile app to the
same topic, and gets pushes for escalations and ready-to-merge units.

If NTFY_TOPIC is not set, all push() calls are no-ops (logged to stderr).
This keeps the orchestrator usable without notifications during testing.
"""

from __future__ import annotations

import os
import sys
from typing import Literal

import httpx

NtfyPriority = Literal["min", "low", "default", "high", "urgent"]

_NTFY_BASE = "https://ntfy.sh"


def is_configured() -> bool:
    return bool(os.getenv("NTFY_TOPIC", "").strip())


def push(
    title: str,
    body: str,
    *,
    priority: NtfyPriority = "default",
    tags: list[str] | None = None,
    click_url: str | None = None,
) -> bool:
    """Send a push notification via ntfy.sh.

    Returns True on success, False if no topic configured or send failed.
    Never raises — notification failures must not break the orchestrator.

    `tags` are emoji shortcodes (https://docs.ntfy.sh/emojis/), e.g.
    ['rotating_light'] for escalations, ['white_check_mark'] for success.
    """
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        print(f"[ntfy disabled — would push] {title}: {body}", file=sys.stderr)
        return False

    headers = {
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url

    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f"{_NTFY_BASE}/{topic}", content=body, headers=headers)
            r.raise_for_status()
        return True
    except Exception as e:
        print(f"[ntfy push failed] {title}: {body} ({e})", file=sys.stderr)
        return False


def push_escalation(unit_id: str, reason: str, pr_url: str | None = None) -> bool:
    """Helper for the most common escalation case."""
    body = f"Unit {unit_id} escalated: {reason}"
    if pr_url:
        body += f"\nPR: {pr_url}"
    return push(
        title=f"🚨 {unit_id} needs you",
        body=body,
        priority="high",
        tags=["rotating_light"],
        click_url=pr_url,
    )


def push_ready_to_merge(unit_id: str, pr_url: str, summary: str = "") -> bool:
    """Helper for 'PR ready, please merge' pushes."""
    body = f"Unit {unit_id} is ready to merge."
    if summary:
        body += f"\n{summary}"
    return push(
        title=f"✅ {unit_id} ready to merge",
        body=body,
        priority="default",
        tags=["white_check_mark"],
        click_url=pr_url,
    )
