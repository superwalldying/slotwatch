"""The only module that talks to Discord.

The webhook URL is a credential: anyone holding it can post to your channel. Every
error path here scrubs it, because these logs land in GitHub Actions output.
"""

from __future__ import annotations

import re
import time
from typing import Callable

import requests

DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 2

_WEBHOOK_RE = re.compile(r"https://discord\.com/api/webhooks/\S+", re.IGNORECASE)


class NotifyError(RuntimeError):
    """Discord rejected the message."""


def redact(text: str) -> str:
    """Never let a webhook URL reach a log line."""
    return _WEBHOOK_RE.sub("https://discord.com/api/webhooks/[REDACTED]", text)


def send(
    payload: dict,
    webhook_url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if not webhook_url:
        raise NotifyError("no webhook URL configured")

    owned = session is None
    session = session or requests.Session()

    try:
        for attempt in range(1, retries + 1):
            try:
                response = session.post(webhook_url, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise NotifyError(f"could not reach Discord: {redact(str(exc))}") from exc
                sleep(1.0 * attempt)
                continue

            if 200 <= response.status_code < 300:
                return

            if response.status_code == 429 and attempt < retries:
                sleep(_retry_after(response))
                continue

            raise NotifyError(
                f"Discord returned HTTP {response.status_code}: "
                f"{redact(response.text[:300])}"
            )
    finally:
        if owned:
            session.close()


def _retry_after(response) -> float:
    try:
        return min(float(response.json().get("retry_after", 1.0)), 30.0)
    except (ValueError, AttributeError, TypeError):
        return 1.0
