"""BrowserSession mixin: navigation, DOM snapshot and AI actions
(goto, reload, iframe, click, fill, select, scroll, wait)."""

import base64
import logging
from .models import DomSnapshot

log = logging.getLogger("ai_browser2")


class PageActionsMixin:
    """Navigation, snapshot and actions of a BrowserSession (mixed into it)."""

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        """Navigate to a URL."""
        log.info("[%s] Navigating to %s", self.sid, url)
        self._original_url = url  # Keep the full URL (before redirects)
        await self.page.goto(url, wait_until=wait_until, timeout=30000)
        self._active_frame = self.page

    async def reload_payment(self):
        """Recovery action: open the same payment URL in a NEW tab, close the
        old one, then re-enter the checkout iframe.

        Much more reliable than page.reload() because Flutterwave's checkout
        sometimes gets stuck on its internal loader after a reload."""
        url = self._original_url or self.page.url
        log.info("[%s] Action: reload_payment — opening new tab for %s", self.sid, url)

        try:
            new_page = await self._new_instrumented_page()

            old_page = self.page
            self.page = new_page
            self._active_frame = new_page
            try:
                await old_page.close()
            except Exception:
                pass

            await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await new_page.wait_for_timeout(5000)

            for _ in range(3):
                try:
                    await self.enter_iframe("iframe")
                    frame = self._active_frame or self.page
                    await frame.wait_for_selector("#phone", timeout=10000)
                    await self.hook_crypto()
                    log.info("[%s] reload_payment: form ready in new tab", self.sid)
                    return True
                except Exception as e:
                    log.warning("[%s] reload_payment re-enter attempt: %s", self.sid, e)
                    self._active_frame = self.page
                    await self.page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("[%s] reload_payment new tab failed: %s", self.sid, e)

        return False

    async def enter_iframe(self, selector: str = "iframe"):
        """Switch context into the first matching iframe."""
        handle = await self.page.wait_for_selector(selector, timeout=15000)
        frame = await handle.content_frame()
        if frame:
            self._active_frame = frame
            log.info("[%s] Entered iframe: %s", self.sid, selector)
            return True
        log.warning("[%s] Could not enter iframe: %s", self.sid, selector)
        return False

    async def snapshot(self, max_html: int = 100_000) -> DomSnapshot:
        """Take a full snapshot: screenshot + DOM + interactive elements."""
        snap = DomSnapshot()
        snap.url = self.page.url

        screenshot_bytes = await self.page.screenshot(full_page=False)
        snap.screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        # Try active frame first, fall back to page if frame is detached
        frame = self._active_frame or self.page
        try:
            html = await frame.content()
        except Exception:
            # Frame was detached/navigated — re-enter iframe or use main page
            self.frame_detached_count += 1
            log.warning("[%s] Active frame detached, re-entering iframe", self.sid)
            try:
                await self.enter_iframe("iframe")
                frame = self._active_frame
                try:
                    await frame.wait_for_selector("#phone", timeout=8000)
                except Exception:
                    pass
                html = await frame.content()
                await self.hook_crypto()
            except Exception:
                frame = self.page
                self._active_frame = self.page
                html = await frame.content()

        if len(html) > max_html:
            html = html[:max_html] + "\n<!-- truncated -->"
        snap.outer_html = html

        # DOM de la page principale (toujours, même quand on est dans un iframe).
        # Critique quand la page redirige vers payment-done : l'IA voit le texte
        # "failed"/"success" affiché là, pas seulement l'URL.
        try:
            page_html = await self.page.content()
            if len(page_html) > 8000:
                page_html = page_html[:8000] + "\n<!-- truncated -->"
            snap.page_outer_html = page_html
        except Exception:
            snap.page_outer_html = ""

        snap.interactive_elements = await self._get_interactive_elements()
        snap.frame_detached_count = self.frame_detached_count

        # Réseau — vision complète comme DevTools.
        pending = self.get_pending_requests(min_age_s=1.0)
        snap.pending_requests = [
            {"type": p.get("resource_type", ""), "url": p["url"][-80:], "age_s": p["age_s"]}
            for p in pending[:10]
        ]
        _ASSET_EXTS = (".js", ".css", ".png", ".svg", ".ttf", ".woff", ".ico")
        snap.recent_responses = [
            {
                "method": r.method,
                "status": r.status,
                "url": r.url[-120:],
                "body": "" if any(r.url.endswith(e) for e in _ASSET_EXTS)
                        else r.response_body[:500],
            }
            for r in self.captured_requests[-20:]
        ]
        snap.failed_requests = [
            {"method": f.get("method", ""), "url": f["url"][-100:], "error": f.get("error", "")}
            for f in list(self.failed_requests)[-5:]
        ]
        signals = self.get_error_signals()
        snap.console_errors = [
            e.get("text", "")[:200] for e in signals.get("console_errors", [])
        ][-5:]

        return snap

    async def _get_interactive_elements(self) -> list[dict]:
        """Extract all interactive elements with their properties."""
        js = """
        (() => {
            const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [onclick]';
            const els = document.querySelectorAll(selectors);
            const results = [];
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const entry = {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    name: el.getAttribute('name') || '',
                    type: el.getAttribute('type') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    value: el.value || '',
                    text: (el.innerText || el.textContent || '').trim().slice(0, 100),
                    href: el.getAttribute('href') || '',
                    classes: el.className || '',
                    visible: rect.width > 0 && rect.height > 0,
                    rect: { x: Math.round(rect.x), y: Math.round(rect.y),
                            w: Math.round(rect.width), h: Math.round(rect.height) }
                };
                // Pour un <select>, expose les options (value + libellé) afin que
                // l'IA sache QUELLE valeur passer à l'action select au lieu de la
                // deviner. Sans ça, elle peut envoyer un libellé ou un objet faux.
                if (el.tagName.toLowerCase() === 'select') {
                    entry.options = Array.from(el.options).map(o => ({
                        value: o.value,
                        label: (o.label || o.textContent || '').trim().slice(0, 80),
                        selected: o.selected,
                    }));
                    // el.value peut valoir "[object Object]" sur certains selects
                    // Vue.js (Flutterwave) — le LLM le recopiait tel quel. On force
                    // value = la value de l'option sélectionnée (ou '' si aucune).
                    const sel = el.selectedOptions && el.selectedOptions[0];
                    entry.value = sel ? sel.value : '';
                }
                results.push(entry);
            }
            return results;
        })()
        """
        frame = self._active_frame or self.page
        try:
            return await frame.evaluate(js)
        except Exception as e:
            log.warning("[%s] Failed to get interactive elements: %s", self.sid, e)
            return []

    # ---- Actions the AI agent can call ----

    async def click(self, selector: str):
        frame = self._active_frame or self.page
        log.info("[%s] Action: click(%s)", self.sid, selector)
        await frame.click(selector, timeout=10000)

    async def fill(self, selector: str, value: str):
        frame = self._active_frame or self.page
        log.info("[%s] Action: fill(%s, %s)", self.sid, selector, value)
        await frame.fill(selector, value, timeout=10000)

    async def select(self, selector: str, value: str):
        """Choisir une option d'un <select>.

        Playwright matche `value` contre la value OU le label de l'option. On
        gère deux formes dégénérées que le LLM produit parfois (sans rien décider
        à sa place — on corrige juste la forme) :
          - "[object Object]" : le LLM a stringifié l'objet option entier ; on
            retombe alors sur un match par LABEL (le libellé que le LLM voulait).
          - value qui ne matche aucune option : on tente un match par label.
        """
        frame = self._active_frame or self.page
        log.info("[%s] Action: select(%s, %s)", self.sid, selector, value)
        is_objobj = (value or "").strip().lower() == "[object object]"
        if not is_objobj:
            try:
                await frame.select_option(selector, value, timeout=10000)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] select(%s) value=%r n'a pas matché (%s) — "
                            "fallback match par label", self.sid, selector, value, e)
        # Fallback (value "[object Object]" ou sans match) : on relit les options
        # réelles et on choisit par LABEL — utile pour les selects Vue.js dont la
        # value est un objet stringifié inutilisable. On sélectionne par INDEX DOM
        # pour ne pas dépendre d'une value cassée. On corrige la forme, pas le fond.
        wanted = (value or "").strip().lower().replace("[object object]", "").strip()
        opts = await frame.eval_on_selector(
            selector,
            "el => Array.from(el.options).map(o => ({value:o.value, "
            "label:(o.label||o.textContent||'').trim()}))",
        )
        for idx, o in enumerate(opts or []):
            lab = (o.get("label") or "").strip().lower()
            if not lab:
                continue
            if wanted and (wanted in lab or lab in wanted):
                await frame.select_option(selector, index=idx, timeout=10000)
                log.info("[%s] select fallback: label %r -> index %d",
                         self.sid, o["label"], idx)
                return
        raise RuntimeError(
            f"select: aucune option ne matche value={value!r} "
            f"(labels: {[o.get('label') for o in (opts or [])]})")

    async def scroll(self, pixels: int = 500):
        frame = self._active_frame or self.page
        log.info("[%s] Action: scroll(%d)", self.sid, pixels)
        await frame.evaluate(f"window.scrollBy(0, {pixels})")

    async def wait(self, selector: str | None = None, ms: int = 2000):
        frame = self._active_frame or self.page
        if selector:
            log.info("[%s] Action: wait for %s", self.sid, selector)
            await frame.wait_for_selector(selector, timeout=ms)
        else:
            log.info("[%s] Action: wait %dms", self.sid, ms)
            await frame.wait_for_timeout(ms)

    async def current_url(self) -> str:
        return self.page.url
