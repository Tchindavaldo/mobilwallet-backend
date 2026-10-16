"""BrowserSession mixin: instrumented page, console/network diagnostics,
in-flight requests, error signals and page-change watching."""

import logging
import time
from playwright.async_api import Page

log = logging.getLogger("ai_browser2")


class SessionDiagnosticsMixin:
    """Event wiring + diagnostics of a BrowserSession (mixed into it)."""

    async def _new_instrumented_page(self) -> Page:
        """Create a new page with ALL browser events attached to this session.

        Single place to wire events — any new page (initial or reload tab) goes
        through here so the AI's snapshot is always complete and isolated.
        """
        page = await self._context.new_page()
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)
        page.on("request", self._on_request_started)
        page.on("requestfinished", self._on_request_finished)
        return page

    def _on_console(self, msg):
        try:
            entry = {
                "type": msg.type,            # 'error', 'warning', 'log', ...
                "text": msg.text,
                "timestamp": time.time(),
            }
            self.console_messages.append(entry)
            if msg.type in ("error", "warning"):
                log.info("[%s] Console[%s]: %s", self.sid, msg.type, msg.text[:200])
        except Exception:
            pass

    def _on_page_error(self, error):
        try:
            self.console_messages.append({
                "type": "pageerror",
                "text": str(error),
                "timestamp": time.time(),
            })
            log.info("[%s] PageError: %s", self.sid, str(error)[:200])
        except Exception:
            pass

    def _on_request_failed(self, request):
        try:
            failure = request.failure
            entry = {
                "url": request.url,
                "method": request.method,
                "error": failure if isinstance(failure, str) else (failure or "failed"),
                "timestamp": time.time(),
            }
            self.failed_requests.append(entry)
            self._inflight.pop(request.url, None)
            log.info("[%s] RequestFailed: %s %s — %s",
                     self.sid, entry["method"], entry["url"][:100], entry["error"])
        except Exception:
            pass

    def _on_request_started(self, request):
        try:
            self._inflight[request.url] = {
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "started_at": time.time(),
            }
        except Exception:
            pass

    def _on_request_finished(self, request):
        try:
            self._inflight.pop(request.url, None)
        except Exception:
            pass

    def get_pending_requests(self, min_age_s: float = 0.0) -> list[dict]:
        """Requests started but never finished/failed (stalled assets).

        `min_age_s` filters to those in flight for at least that long — useful
        to flag truly stuck assets (e.g. JS bundles) vs requests just started.
        """
        now = time.time()
        out = []
        for entry in self._inflight.values():
            age = now - entry["started_at"]
            if age >= min_age_s:
                out.append({**entry, "age_s": round(age, 1)})
        return sorted(out, key=lambda e: e["age_s"], reverse=True)

    def reset_diagnostics(self):
        """Clear console + failed-request buffers (call before clicking Pay)."""
        self.console_messages.clear()
        self.failed_requests.clear()

    def get_error_signals(self) -> dict:
        """Collect error signals from console + network for LLM interpretation.

        Returns console errors/warnings, failed network requests, and any
        HTTP error responses (status >= 400) captured during the charge.
        """
        # Drop benign console noise that is not a payment signal.
        _NOISE = (
            "autofocus", "favicon", "mixpanel", "preload",
            "download the react", "deprecat", "sourcemap",
            "google analytics", "gtag", "stripe.js",
        )
        console_errors = [
            m for m in self.console_messages
            if m["type"] in ("error", "warning", "pageerror")
            and not any(n in m["text"].lower() for n in _NOISE)
        ]
        http_errors = [
            {"url": r.url, "status": r.status, "body": r.response_body[:500]}
            for r in self.captured_requests
            if r.status >= 400
        ]
        return {
            "console_errors": console_errors,
            "failed_requests": list(self.failed_requests),
            "http_errors": http_errors,
        }

    async def watch_page_changes(self):
        """Install a MutationObserver to detect page changes in real-time."""
        frame = self._active_frame or self.page
        try:
            await frame.evaluate("""
                (function() {
                    if (window._pageWatcherInstalled) return;
                    window._pageWatcherInstalled = true;
                    window._pageStatus = null;

                    var lastText = (document.body.innerText || '').substring(0, 500);
                    const observer = new MutationObserver(function(mutations) {
                        var currentText = (document.body.innerText || '').substring(0, 500);
                        if (currentText !== lastText) {
                            window._pageStatus = {
                                status: 'changed',
                                message: currentText,
                                previous: lastText,
                                timestamp: Date.now()
                            };
                            lastText = currentText;
                        }
                    });
                    observer.observe(document.body, {childList: true, subtree: true, characterData: true});

                    var lastUrl = location.href;
                    setInterval(function() {
                        if (location.href !== lastUrl) {
                            window._pageStatus = {status: 'redirected', message: 'Redirected to ' + location.href, timestamp: Date.now()};
                            lastUrl = location.href;
                        }
                    }, 500);
                })();
            """)
            log.info("[%s] Page change watcher installed", self.sid)
        except Exception as e:
            log.warning("[%s] Failed to install page watcher: %s", self.sid, e)

    async def get_page_status(self) -> dict | None:
        """Check if the page watcher detected a status change."""
        frame = self._active_frame or self.page
        try:
            return await frame.evaluate("window._pageStatus")
        except Exception:
            return None

    async def wait_for_page_change(self, timeout_s: float, baseline: str | None = None):
        """OBSERVATION PURE: bloque jusqu'à ce que la page change, sans rien juger.

        Installe le watcher (idempotent), puis poll son drapeau jusqu'à détecter
        un texte différent de `baseline` (l'état déjà vu par l'IA) OU une
        redirection d'URL, OU l'expiration de `timeout_s`. Ne décide RIEN: retourne
        juste le nouveau texte observé (str) ou None si rien n'a changé dans le
        budget. C'est à l'IA d'interpréter ce texte au tour suivant.
        """
        import asyncio
        await self.watch_page_changes()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(1)
            st = await self.get_page_status()
            if st and st.get("status") in ("changed", "redirected"):
                txt = st.get("message", "")
                if txt and txt != baseline:
                    log.info("[%s] 👁 polling — page changée, texte vu:\n%s",
                             self.sid, txt[:1000])
                    return txt
            # Redirection hors checkout = page de résultat (fait observable).
            try:
                url = self.page.url
                if ("flutterwave.com" not in url
                        and "checkout-v3-ui-prod" not in url
                        and "ravepay" not in url):
                    log.info("[%s] 👁 polling — redirection hors checkout: %s", self.sid, url)
                    return f"redirected to {url}"
            except Exception:
                pass
        return None
