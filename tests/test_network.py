"""Contract tests for the two network edges.

Kept deliberately thin: these modules exist so the rest of the codebase can stay pure.
What matters is that the request shape is right and that failures are neither silent nor
leaky - not exhaustive coverage of requests' own behaviour.
"""

from __future__ import annotations

import pytest
import responses

from slotwatch.fetch import FetchError, fetch_tab
from slotwatch.notify import NotifyError, redact, send

from .conftest import SITE

WEBHOOK = "https://discord.com/api/webhooks/123456/super-secret-token"

PRIMARY = SITE.tabs["primary"]


def no_sleep(_seconds: float) -> None:
    """Keep retry tests instant."""


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


@responses.activate
def test_fetch_posts_the_request_the_site_expects():
    responses.add(responses.POST, SITE.ajax_url, body="<table></table>", status=200)

    fetch_tab(PRIMARY, SITE, sleep=no_sleep)

    request = responses.calls[0].request
    assert request.method == "POST"
    body = dict(pair.split("=", 1) for pair in request.body.split("&"))
    assert body["action"] == SITE.action
    assert body["buttonid"] == "5"
    assert body["filterid"] == "18"
    assert body["gametypeid"] == "1"


@responses.activate
def test_fetch_identifies_itself_from_the_site_profile():
    responses.add(responses.POST, SITE.ajax_url, body="ok", status=200)

    fetch_tab(PRIMARY, SITE, sleep=no_sleep)

    headers = responses.calls[0].request.headers
    assert headers["User-Agent"] == SITE.user_agent
    assert headers["Referer"] == SITE.referer


@responses.activate
def test_fetch_returns_the_body():
    responses.add(responses.POST, SITE.ajax_url, body="<table>rows</table>", status=200)

    assert fetch_tab(PRIMARY, SITE, sleep=no_sleep) == "<table>rows</table>"


@responses.activate
def test_fetch_retries_a_server_error_then_succeeds():
    responses.add(responses.POST, SITE.ajax_url, body="boom", status=503)
    responses.add(responses.POST, SITE.ajax_url, body="recovered", status=200)

    assert fetch_tab(PRIMARY, SITE, sleep=no_sleep) == "recovered"
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_raises_after_exhausting_retries():
    for _ in range(3):
        responses.add(responses.POST, SITE.ajax_url, body="boom", status=503)

    with pytest.raises(FetchError, match="giving up"):
        fetch_tab(PRIMARY, SITE, retries=3, sleep=no_sleep)


@responses.activate
def test_fetch_does_not_retry_a_client_error():
    responses.add(responses.POST, SITE.ajax_url, body="nope", status=404)

    with pytest.raises(FetchError, match="not retried"):
        fetch_tab(PRIMARY, SITE, retries=3, sleep=no_sleep)

    assert len(responses.calls) == 1


def test_every_tab_has_a_distinct_buttonid_filterid_pair():
    pairs = {(t.buttonid, t.filterid) for t in SITE.tabs.values()}

    assert len(pairs) == len(SITE.tabs)


def test_unknown_tab_key_is_rejected():
    from slotwatch.site import SiteConfigError

    with pytest.raises(SiteConfigError, match="unknown tab"):
        SITE.tab("atlantis_tuesday")


# --------------------------------------------------------------------------
# notify
# --------------------------------------------------------------------------


@responses.activate
def test_send_posts_the_payload_as_json():
    responses.add(responses.POST, WEBHOOK, status=204)

    send({"content": "hi"}, WEBHOOK, sleep=no_sleep)

    assert responses.calls[0].request.body == b'{"content": "hi"}'


@responses.activate
def test_send_raises_on_rejection():
    responses.add(responses.POST, WEBHOOK, body="bad request", status=400)

    with pytest.raises(NotifyError, match="400"):
        send({"content": "hi"}, WEBHOOK, sleep=no_sleep)


@responses.activate
def test_send_retries_once_on_rate_limit():
    responses.add(responses.POST, WEBHOOK, json={"retry_after": 0.1}, status=429)
    responses.add(responses.POST, WEBHOOK, status=204)

    send({"content": "hi"}, WEBHOOK, sleep=no_sleep)

    assert len(responses.calls) == 2


def test_send_without_a_url_fails_loudly():
    with pytest.raises(NotifyError, match="no webhook URL"):
        send({"content": "hi"}, "", sleep=no_sleep)


@responses.activate
def test_error_messages_never_leak_the_webhook_url():
    """These logs land in CI output, where the URL would be a credential leak."""
    responses.add(responses.POST, WEBHOOK, body=f"failed calling {WEBHOOK}", status=500)

    with pytest.raises(NotifyError) as excinfo:
        send({"content": "hi"}, WEBHOOK, retries=1, sleep=no_sleep)

    assert "super-secret-token" not in str(excinfo.value)
    assert "REDACTED" in str(excinfo.value)


def test_redact_scrubs_webhook_urls_anywhere_in_a_string():
    assert "super-secret-token" not in redact(f"boom at {WEBHOOK} while posting")
