"""Playwright browser controller — screenshots, DOM, actions, network capture.

Concurrency model (feature/browser-concurrency):
  - `BrowserSession` = ONE tab + all the per-transaction state (network capture,
    active frame, console/inflight diagnostics, event handlers). Every action and
    snapshot lives here, so two payments running at once never corrupt each other.
  - `BrowserController` = a POOL: it owns the Playwright `Browser` instance(s),
    hands out isolated sessions (`acquire_session`) and reclaims them
    (`release_session`). When a Chrome reaches `max_tabs` open tabs, a fresh Chrome
    is launched for the next session.

Package layout: models.py, session.py (+ mixins diagnostics.py, capture.py,
actions.py), controller.py. Public names are re-exported here.
"""

from .models import CapturedRequest, DomSnapshot
from .session import BrowserSession
from .controller import BrowserController

__all__ = ["CapturedRequest", "DomSnapshot", "BrowserSession", "BrowserController"]
