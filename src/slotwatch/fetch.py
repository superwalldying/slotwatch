"""The only module that talks to the watched site.

Why this posts to the AJAX endpoint rather than fetching a normal page URL: the public
page only ever server-renders *tab 1* of a program group. Every other tab, including the
one we care about, loads exclusively through this POST - the identical request the site's
own front-end fires on each tab click. Probing the page's filter parameter across its
whole range found no value that renders the wanted tab directly, so there is no
alternative path.

Kept deliberately gentle: one request per poll, honest User-Agent (verified acceptable -
no browser impersonation needed), jittered intervals, exponential backoff, and no retries
on 4xx. Target details come from the runtime site profile (see site.py), never from here.
"""

from __future__ import annotations

import random
import time
from typing import Callable

import requests

from .site import Site, Tab

DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0


class FetchError(RuntimeError):
    """Network or HTTP failure that survived all retries."""


def build_session(site: Site) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": site.user_agent,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": site.referer,
            "Accept": "text/html, */*; q=0.01",
        }
    )
    return session


def fetch_tab(
    tab: Tab,
    site: Site,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return the raw HTML fragment for one tab, retrying transient failures."""
    owned = session is None
    session = session or build_session(site)
    payload = {
        "action": site.action,
        "buttonid": str(tab.buttonid),
        "gametypeid": str(tab.gametypeid),
        "filterid": str(tab.filterid),
    }

    last_error: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                response = session.post(site.ajax_url, data=payload, timeout=timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    return response.text
                # 4xx means we're asking wrongly; retrying won't help.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise FetchError(
                        f"{tab.key}: HTTP {response.status_code} (not retried)"
                    )
                last_error = FetchError(f"{tab.key}: HTTP {response.status_code}")

            if attempt < retries:
                # Jitter keeps repeated failures from hammering in lockstep.
                sleep(backoff * (2 ** (attempt - 1)) * random.uniform(0.5, 1.5))

        raise FetchError(f"{tab.key}: giving up after {retries} attempts ({last_error})")
    finally:
        if owned:
            session.close()
