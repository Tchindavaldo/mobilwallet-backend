"""BrowserController: pool of Chrome instances handing out isolated
BrowserSessions (acquire/release, new Chrome once max_tabs is reached)."""

import logging
from playwright.async_api import async_playwright, Browser, BrowserContext
from .session import BrowserSession

log = logging.getLogger("ai_browser2")


class BrowserController:
    """Pool of Chrome instances handing out isolated `BrowserSession`s.

    Owns the Playwright runtime and one or more `Browser` instances. Each Chrome
    holds up to `max_tabs` open tabs; once full, a new Chrome is launched for the
    next session. Sessions are isolated so concurrent payments never collide.
    """

    def __init__(self, headless: bool = False, max_tabs: int = 20):
        self._headless = headless
        self.max_tabs = max_tabs
        self._pw = None
        # Each pool entry: {"browser": Browser, "context": BrowserContext, "open": int}
        self._browsers: list[dict] = []
        self._sid_counter = 0

    async def start(self):
        """Launch the first Chrome so the service is ready at boot."""
        self._pw = await async_playwright().start()
        await self._launch_browser()
        log.info("Browser pool started (headless=%s, max_tabs=%d)",
                 self._headless, self.max_tabs)

    async def _launch_browser(self) -> dict:
        launch_kwargs = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # In GUI mode, use installed Chrome to avoid spawn issues with the
        # bundled Chromium.
        if not self._headless:
            launch_kwargs["channel"] = "chrome"
        browser = await self._pw.chromium.launch(**launch_kwargs)
        # Pas de contexte partagé ici : chaque session crée le SIEN dans
        # acquire_session (isolation cookies + fermeture sans impact sur les
        # autres). 'open' compte les sessions/contextes actifs de ce Chrome.
        entry = {"browser": browser, "open": 0}
        self._browsers.append(entry)
        log.info("Launched Chrome #%d (pool size=%d)", len(self._browsers), len(self._browsers))
        return entry

    async def acquire_session(self) -> BrowserSession:
        """Open an isolated session (its own BrowserContext) for one transaction.

        Picks a Chrome with spare capacity (< max_tabs sessions); launches a new
        Chrome if all current ones are full. Each session gets a DEDICATED
        context so closing one never affects another (no shared cookies/tabs).
        """
        entry = next((b for b in self._browsers if b["open"] < self.max_tabs), None)
        if entry is None:
            entry = await self._launch_browser()
        context = await entry["browser"].new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        self._sid_counter += 1
        sid = f"s{self._sid_counter}"
        session = BrowserSession(self, context, sid)
        session._pool_entry = entry
        await session.open()
        entry["open"] += 1
        log.info("[%s] Session acquise (Chrome a %d/%d sessions)",
                 sid, entry["open"], self.max_tabs)
        return session

    async def release_session(self, session: BrowserSession):
        """Close the session's tab and free its slot in the pool."""
        await session.close()
        entry = getattr(session, "_pool_entry", None)
        if entry is not None:
            entry["open"] = max(0, entry["open"] - 1)
            log.info("[%s] Session libérée (Chrome a %d/%d sessions)",
                     session.sid, entry["open"], self.max_tabs)

    async def stop(self):
        for entry in self._browsers:
            try:
                await entry["browser"].close()
            except Exception:
                pass
        self._browsers.clear()
        if self._pw:
            await self._pw.stop()
        log.info("Browser pool stopped")
