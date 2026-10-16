"""BrowserSession mixin: network capture (crypto hook, captured requests,
charge/verify lookup, response listener)."""

import logging
import time
from collections.abc import Callable
from playwright.async_api import Response
from .models import CapturedRequest

log = logging.getLogger("ai_browser2")


class NetworkCaptureMixin:
    """Network capture of a BrowserSession (mixed into it)."""

    async def hook_crypto(self):
        """Monkey-patch cryptico.encrypt to capture plaintext before encryption.
        Must be called AFTER the iframe content has loaded cryptico.js."""
        frame = self._active_frame or self.page
        try:
            await frame.evaluate("""
                (function() {
                    function patchCryptico() {
                        if (typeof window.cryptico === 'undefined' ||
                            typeof window.cryptico.encrypt !== 'function') {
                            return false;
                        }
                        if (window._crypticoPatched) return true;
                        const originalEncrypt = window.cryptico.encrypt;
                        window._capturedPlaintexts = [];
                        window.cryptico.encrypt = function(plaintext, publicKey) {
                            try {
                                window._capturedPlaintexts.push({
                                    timestamp: Date.now(),
                                    plaintext: plaintext,
                                    publicKey: publicKey
                                });
                            } catch(e) {}
                            return originalEncrypt.apply(this, arguments);
                        };
                        window._crypticoPatched = true;
                        return true;
                    }
                    if (!patchCryptico()) {
                        var attempts = 0;
                        var timer = setInterval(function() {
                            attempts++;
                            if (patchCryptico() || attempts > 50) {
                                clearInterval(timer);
                            }
                        }, 200);
                    }
                })();
            """)
            log.info("[%s] Crypto hook installed in active frame", self.sid)
        except Exception as e:
            log.warning("[%s] Failed to install crypto hook: %s", self.sid, e)

    async def get_captured_plaintexts(self) -> list[dict]:
        """Retrieve any plaintext data captured by the crypto hook."""
        frame = self._active_frame or self.page
        try:
            result = await frame.evaluate("window._capturedPlaintexts || []")
            if result:
                log.info("[%s] Captured %d plaintext payloads", self.sid, len(result))
            return result
        except Exception:
            return []

    def start_capture(self, url_filter: str = ""):
        """Start capturing network requests. Optional URL substring filter."""
        self.captured_requests.clear()
        self._capture_filter = url_filter
        self._capture_enabled = True
        log.info("[%s] Network capture started (filter=%s)", self.sid, url_filter or "*")

    def stop_capture(self) -> list[CapturedRequest]:
        """Stop capturing and return captured requests."""
        self._capture_enabled = False
        log.info("[%s] Network capture stopped: %d requests", self.sid, len(self.captured_requests))
        return self.captured_requests.copy()

    def get_charge_request(self, matcher: Callable[[CapturedRequest], bool]) -> CapturedRequest | None:
        """Find the first captured request matching `matcher` (the aggregator's
        charge predicate). Generic — each aggregator supplies its own matcher."""
        for r in self.captured_requests:
            if matcher(r):
                return r
        return None

    def get_verify_requests(self, matcher: Callable[[CapturedRequest], bool]) -> list[CapturedRequest]:
        """Find all captured requests matching `matcher` (the aggregator's verify
        predicate). Generic — each aggregator supplies its own matcher."""
        return [r for r in self.captured_requests if matcher(r)]

    async def _on_response(self, response: Response):
        """Callback for every network response."""
        if not self._capture_enabled:
            return

        request = response.request
        url = request.url

        if self._capture_filter and self._capture_filter not in url:
            return

        cap = CapturedRequest()
        cap.url = url
        cap.method = request.method
        cap.status = response.status
        cap.timestamp = time.time()

        try:
            cap.request_headers = dict(await request.all_headers())
        except Exception:
            cap.request_headers = {}

        try:
            cap.request_body = request.post_data or ""
        except Exception:
            cap.request_body = ""

        try:
            cap.response_headers = dict(await response.all_headers())
        except Exception:
            cap.response_headers = {}

        try:
            body_bytes = await response.body()
            cap.response_body = body_bytes.decode("utf-8", errors="replace")[:2000]
        except Exception:
            cap.response_body = ""

        self.captured_requests.append(cap)
        log.info("[%s] Captured: %s %s [%d] body=%d",
                 self.sid, cap.method, cap.url[:100], cap.status, len(cap.response_body))
