"""Which site to watch, and how to address it.

Every identifying detail lives here and is loaded at runtime, never committed. Supply it
either as `site.yaml` (gitignored) or via the `SITE_PROFILE` environment variable holding
the same YAML - the latter is how CI passes it in from a secret.

See site.example.yaml for the shape. This keeps the published repository free of the
target's name, domain, endpoint and venue labels, while the scraper itself stays generic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import yaml

SITE_ENV = "SITE_PROFILE"
DEFAULT_SITE_FILE = Path("site.yaml")

# Structural tokens. Not identifying on their own - they are ordinary WordPress-ish
# markup - and the parser cannot work without them, so they carry sane defaults.
DEFAULT_FIELD_NAME = "f_GameID"
DEFAULT_EMPTY_MARKER = "no open session"


class SiteConfigError(RuntimeError):
    """No usable site profile was found."""


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    buttonid: int
    filterid: int
    gametypeid: int = 1
    # Optional per-tab booking page; falls back to the site-wide book_url.
    book_url: str | None = None


@dataclass(frozen=True)
class Site:
    ajax_url: str
    action: str
    book_url: str
    referer: str
    tabs: dict[str, Tab]
    field_name: str = DEFAULT_FIELD_NAME
    empty_marker: str = DEFAULT_EMPTY_MARKER
    user_agent: str = (
        "slotwatch/0.1 (personal booking-slot watcher; low-frequency, 1 request per poll)"
    )

    def tab_display(self) -> dict[str, tuple[str, str]]:
        """tab key -> (label, booking url), for grouping alerts per venue."""
        return {
            key: (tab.label, tab.book_url or self.book_url)
            for key, tab in self.tabs.items()
        }

    def tab(self, key: str) -> Tab:
        if key not in self.tabs:
            raise SiteConfigError(
                f"unknown tab {key!r}; site profile defines {sorted(self.tabs)}"
            )
        return self.tabs[key]


def _build(raw: dict) -> Site:
    missing = [k for k in ("base_url", "action", "tabs") if not raw.get(k)]
    if missing:
        raise SiteConfigError(f"site profile is missing required key(s): {missing}")

    base_url = str(raw["base_url"]).rstrip("/") + "/"
    ajax_path = str(raw.get("ajax_path", "wp-admin/admin-ajax.php")).lstrip("/")
    ajax_url = str(raw.get("ajax_url") or urljoin(base_url, ajax_path))
    book_url = str(raw.get("book_url") or base_url)

    tabs: dict[str, Tab] = {}
    for key, entry in (raw.get("tabs") or {}).items():
        tabs[str(key)] = Tab(
            key=str(key),
            label=str(entry.get("label", key)),
            buttonid=int(entry["buttonid"]),
            filterid=int(entry["filterid"]),
            gametypeid=int(entry.get("gametypeid", raw.get("gametypeid", 1))),
            book_url=(str(entry["book_url"]) if entry.get("book_url") else None),
        )
    if not tabs:
        raise SiteConfigError("site profile defines no tabs")

    site = Site(
        ajax_url=ajax_url,
        action=str(raw["action"]),
        book_url=book_url,
        referer=str(raw.get("referer") or book_url),
        tabs=tabs,
        field_name=str(raw.get("field_name", DEFAULT_FIELD_NAME)),
        empty_marker=str(raw.get("empty_marker", DEFAULT_EMPTY_MARKER)).casefold(),
    )
    if raw.get("user_agent"):
        site = Site(**{**site.__dict__, "user_agent": str(raw["user_agent"])})
    return site


def load_site(path: str | Path | None = None) -> Site:
    """Environment variable first, so CI needs no extra file-writing step."""
    inline = os.environ.get(SITE_ENV)
    if inline and inline.strip():
        parsed = yaml.safe_load(inline)
        if not isinstance(parsed, dict):
            raise SiteConfigError(f"{SITE_ENV} did not contain a YAML mapping")
        return _build(parsed)

    candidate = Path(path) if path else DEFAULT_SITE_FILE
    if not candidate.exists():
        raise SiteConfigError(
            f"no site profile: set {SITE_ENV} or create {candidate}. "
            "Copy site.example.yaml and fill it in."
        )
    parsed = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise SiteConfigError(f"{candidate} did not contain a YAML mapping")
    return _build(parsed)
