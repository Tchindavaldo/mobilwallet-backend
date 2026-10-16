"""BrowserSession: one isolated tab/context + its per-transaction state.
Behaviour is split across mixins (diagnostics, capture, actions)."""

import logging
from playwright.async_api import BrowserContext, Page
from typing import TYPE_CHECKING
from .actions import PageActionsMixin
from .capture import NetworkCaptureMixin
from .diagnostics import SessionDiagnosticsMixin
from .models import CapturedRequest

if TYPE_CHECKING:
    from .controller import BrowserController

log = logging.getLogger("ai_browser2")


class BrowserSession(SessionDiagnosticsMixin, NetworkCaptureMixin, PageActionsMixin):
    """One isolated tab + its full per-transaction state.

    Created and owned by `BrowserController`. The reasoning loop and the runner
    drive a session (not the global controller), so concurrent payments stay
    isolated: each has its own captured_requests, active frame, console buffer,
    inflight map and frame-detach counter.
    """

    def __init__(self, controller: "BrowserController", context: BrowserContext, sid: str):
        self._controller = controller
        self._context = context
        self.sid = sid
        self.page: Page | None = None
        self._active_frame = None
        # Network capture
        self.captured_requests: list[CapturedRequest] = []
        self._capture_enabled = False
        self._capture_filter = ""
        # Console messages + network failures (for error detection)
        self.frame_detached_count: int = 0
        self.console_messages: list[dict] = []
        self.failed_requests: list[dict] = []
        # In-flight tracking: requests started but not yet finished/failed.
        self._inflight: dict[str, dict] = {}
        # Navigation memory
        self._original_url: str | None = None

    async def open(self):
        """Create the tab and wire every browser event to THIS session."""
        self.page = await self._new_instrumented_page()
        self._active_frame = self.page
        return self

    async def close(self):
        """Fermer le CONTEXTE isolé de cette session (la contrôleur appelle au
        release). Chaque session a son propre BrowserContext, donc fermer le
        sien NE touche AUCUNE autre session (cookies/onglets séparés). On ferme
        le contexte entier (pas juste la page) pour ne rien laisser fuir."""
        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            log.warning("[%s] close() context exception: %s", self.sid, e)
