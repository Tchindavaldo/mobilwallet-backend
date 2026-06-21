"""DigiKUNTZ aggregator — implements the core Aggregator interface.

Adapter that exposes the existing browser_flow (AI-driven) and replay_flow
(no-browser curl replay) behind the common `Aggregator` ABC, and registers
itself in the registry under the name "digikuntz".
"""

import json
import logging

from core.base import Aggregator, CurlTemplate, PaymentRequest, PaymentResult
from core.browser import CapturedRequest, BrowserSession
from core.browser_runner import run_browser_flow
from core.config import settings
from core.upstream_errors import classify_upstream_error
from core.registry import register

from . import browser_flow
from . import replay_flow

log = logging.getLogger("ai_browser2")




class DigikuntzAggregator(Aggregator):
    name = "digikuntz"
    # Canonical network values expected by DigiKUNTZ/Flutterwave.
    supported_networks = ["Orangemoney", "MTN"]

    @property
    def _agent(self) -> "browser_flow.DigikuntzAgent":
        # Reuse one agent bound to this aggregator's browser/llm for callbacks.
        agent = getattr(self, "_agent_cache", None)
        if agent is None:
            agent = browser_flow.DigikuntzAgent(self.browser, self.llm)
            self._agent_cache = agent
        return agent

    # --- transaction creation ---
    async def create_transaction(self, req: PaymentRequest) -> dict:
        return await self._agent._create_transaction(req)

    # --- browser-IA hooks ---
    def browser_objective(self, req: PaymentRequest) -> str:
        return self._agent.browser_objective(req)

    async def decide_browser_outcome(self, req, loop_result, result, session=None) -> None:
        await self._agent.decide_browser_outcome(req, loop_result, result, session=session)

    async def finalize_after_close(self, req, result) -> None:
        await self._agent.finalize_after_close(req, result)

    def network_label(self, network: str) -> str:
        return replay_flow.network_label(network)

    def charge_request_matcher(self, r: CapturedRequest) -> bool:
        return BrowserSession._flutterwave_charge_matcher(r)

    def verify_request_matcher(self, r: CapturedRequest) -> bool:
        return BrowserSession._flutterwave_verify_matcher(r)

    def checkout_url_predicate(self, url: str) -> bool:
        """True while the URL is still on the Flutterwave checkout (not redirected)."""
        return any(
            host in url
            for host in ("flutterwave.com", "checkout-v3-ui-prod", "ravepay")
        )

    # --- template extraction (browser mode -> DB) ---
    def extract_curl_template(
        self, charge: CapturedRequest, verify: "CapturedRequest | None", public_key: str
    ) -> CurlTemplate | None:
        if not charge:
            return None
        # Best-effort payload skeleton from the captured charge body (keys only,
        # values blanked) so replay can re-fill it from the request.
        payload_skeleton: dict = {}
        try:
            body = json.loads(charge.request_body) if charge.request_body else {}
            if isinstance(body, dict):
                payload_skeleton = {k: "" for k in body}
        except (json.JSONDecodeError, TypeError):
            pass
        return CurlTemplate(
            charge_url=charge.url or settings.digikuntz.flw_charge_url,
            verify_url=verify.url if verify else settings.digikuntz.flw_verify_url,
            init_url=settings.digikuntz.flw_init_url,
            upgrade_url=settings.digikuntz.flw_upgrade_url,
            hosted_pay_url="https://api.ravepay.co/flwv3-pug/getpaidx/api/hosted_pay",
            headers=dict(replay_flow.HEADERS),
            payload_skeleton=payload_skeleton,
            public_key_rsa=public_key,
            flw_pub_key=settings.digikuntz.flw_pub_key,
        )

    # --- result interpretation ---
    def interpret_status(self, stage: str, resp: dict, network: str):
        if stage == "charge":
            return replay_flow.interpret_charge(resp, network)
        if stage == "verify":
            return replay_flow.interpret_verify(resp, network)
        if stage == "ping":
            return replay_flow.interpret_ping(resp, network)
        return None

    # --- browser mode (full AI-driven flow) ---
    async def pay_via_browser(self, req: PaymentRequest, tx_id: int | None = None) -> PaymentResult:
        return await run_browser_flow(self, req, tx_id=tx_id)

    async def _mark_template(self, template: "CurlTemplate | None", status: str,
                             reason: str = "") -> None:
        """Marque en BD la fiabilité du template chargé (working/failed).

        No-op si le template n'a pas d'id BD (ex. defaults sans persistance) ou
        si la couche db est absente. Le 'reason' n'est que loggé (jamais exposé).
        """
        db_id = getattr(template, "db_id", None)
        if not db_id or not self.db:
            return
        log.info("[REPLAY] template id=%s marqué '%s'%s",
                 db_id, status, f" ({reason})" if reason else "")
        await self.db.mark_template_status(db_id, status)

    # --- replay mode (no browser) ---
    async def replay(self, req: PaymentRequest, template: CurlTemplate) -> PaymentResult:
        """Reproduce the payment using replay_flow steps (no browser).

        The stored template is turned into a per-call ReplayConfig passed
        explicitly to step2/3/4 — no module globals are mutated, so concurrent
        replays never share state. step1..step4 keep their tuned retry/poll logic.
        """
        result = PaymentResult()
        # Per-call config from the template (URLs/headers/pubkey). Isolated.
        cfg = replay_flow.ReplayConfig.from_template(template)
        log.info("[REPLAY] === début replay ===")
        log.info("[REPLAY] template présent=%s charge_url=%r verify_url=%r init_url=%r",
                 template is not None,
                 getattr(template, "charge_url", ""),
                 getattr(template, "verify_url", ""),
                 getattr(template, "init_url", ""))
        log.info("[REPLAY] template.public_key_rsa(len=%d) flw_pub_key=%r",
                 len(getattr(template, "public_key_rsa", "") or ""),
                 getattr(template, "flw_pub_key", ""))
        try:
            tx = await replay_flow.step1_create_transaction(
                req.amount, req.phone, req.email, req.sender_name,
                raison=req.raison,
            )
        except Exception as e:  # noqa: BLE001 — surface any creation failure
            result.error = f"digikuntz create_transaction error: {e}"
            result.error_code = classify_upstream_error(e) or ""
            return result

        tx_ref = tx.get("transactionRef", "")
        payment_link = tx.get("paymentLink", "")
        total = int(tx.get("paymentWithTaxes", req.amount))
        result.transaction_id = tx_ref
        if not payment_link:
            result.error = f"No paymentLink in response: {tx}"
            return result

        # RSA public key: prefer the stored template, else (re)initialize.
        public_key_rsa = template.public_key_rsa if template else ""
        log.info("[REPLAY] clé RSA depuis template=%s (len=%d)",
                 bool(public_key_rsa), len(public_key_rsa or ""))
        if not public_key_rsa:
            log.info("[REPLAY] pas de clé dans le template -> step2_initialize_checkout (fresh)")
            try:
                checkout = await replay_flow.step2_initialize_checkout(payment_link, cfg=cfg)
                public_key_rsa = checkout.get("public_key", "")
                log.info("[REPLAY] clé RSA fraîche obtenue (len=%d)", len(public_key_rsa or ""))
            except Exception as e:  # noqa: BLE001
                log.warning("replay step2 failed (%s), will rely on fallback key", e)

        charge = await replay_flow.step3_charge(
            amount=total,
            phone=req.phone,
            network=req.network,
            email=req.email,
            firstname="API",
            lastname=f"Call: {req.sender_name}",
            tx_ref=tx_ref,
            public_key_rsa=public_key_rsa,
            cfg=cfg,
        )
        charge_resp = charge.get("charge_response", {})
        result.flutterwave_charge_response = str(charge_resp)[:1000]

        verdict = self.interpret_status("charge", charge_resp, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
            result.success = result.final_status == "successful"
            result.payment_status = result.final_status
            # Erreur TECHNIQUE due au template (clé absente/invalide, chiffrement
            # KO) : ce template est inexploitable -> on le marque 'failed' pour
            # ne plus le réutiliser. Le prochain replay prendra un autre template
            # working/untested (ou exigera un run browser). Le détail reste en
            # LOG (déjà tracé), pas exposé dans la réponse client.
            if result.final_status == "error":
                await self._mark_template(template, "failed",
                                          reason=result.final_message)
                # Détail technique -> LOG seulement. Réponse client : message
                # neutre, sans exposer clé/chiffrement/template.
                log.warning("[REPLAY] échec technique template (-> failed): %s",
                            result.final_message)
                result.final_message = (
                    "Paiement momentanément indisponible. Réessayez."
                )
                result.error = ""
            else:
                # Verdict métier (failed/network_down/...) : le chiffrement a été
                # accepté et la charge a bien été envoyée -> le template fonctionne
                # mécaniquement. On le marque 'working' (l'échec vient de
                # l'opérateur/compte, pas du template).
                await self._mark_template(template, "working")
            return result

        # Extract flw_ref then poll verify. Le flw_ref peut être à plusieurs
        # profondeurs selon la forme de la réponse /charge :
        #   - charge direct  : data.flw_ref  OU  data.data.flw_reference
        #   - "demande longue" (résolu via ping_url) : data.flw_reference
        # On cherche flw_ref ET flw_reference, au niveau data ET data.data.
        charge_data = charge_resp.get("data", {})
        flw_ref = ""
        if isinstance(charge_data, dict):
            flw_ref = (charge_data.get("flw_ref")
                       or charge_data.get("flw_reference") or "")
            if not flw_ref:
                nested = charge_data.get("data", {})
                if isinstance(nested, dict):
                    flw_ref = (nested.get("flw_reference")
                               or nested.get("flw_ref") or "")
        if not flw_ref:
            # Pas de flw_ref ni de verdict exploitable : on ne sait pas trancher
            # (réseau opérateur réel vs template/charge anormal). Détail en LOG,
            # message client neutre, statut 'error'. On ne marque PAS le template
            # 'failed' (ambigu).
            log.warning("[REPLAY] pas de flw_ref dans la réponse /charge: %s",
                        str(charge_resp)[:500])
            result.final_status = "error"
            result.final_message = "Paiement momentanément indisponible. Réessayez."
            result.payment_status = "error"
            return result

        # /charge a renvoyé un flw_ref => l'USSD vient d'être envoyé au client.
        # Le template a donc mécaniquement fonctionné (chiffrement accepté,
        # charge passée) : on le marque 'working' pour les prochains replays.
        await self._mark_template(template, "working")
        # On horodate cet instant : la fenêtre opérateur (base du calcul
        # anti-doublon) court à partir de là.
        import time as _t
        result.ussd_sent_at = _t.time()
        log.info("USSD envoyé (replay, flw_ref=%s)", flw_ref)

        # L'USSD est parti : on REND LA MAIN immédiatement avec un statut
        # provisoire 'ussd_sent'. Le verdict réel (validation/refus) viendra de
        # finalize_after_close (poll verify), exécuté en tâche de fond par /pay,
        # qui notifiera le client via webhook + Socket.IO. On pose tout ce qu'il
        # faut pour rejouer le poll hors de cette requête (verify_params + cfg).
        result.poll_after_close = {
            "verify_params": {
                "modalauditid": charge["modalauditid"],
                "flw_ref": flw_ref,
                "pub_key": cfg.pub_key,
            },
            "cfg": cfg,
            "template_db_id": getattr(template, "db_id", None),
        }
        result.final_status = "ussd_sent"
        result.final_message = replay_flow.ussd_message(req.network)
        result.payment_status = result.final_status
        # USSD bien envoyé = succès d'étape (le client doit maintenant valider).
        result.success = True
        return result

    async def finalize_after_close_replay(self, req, result) -> None:
        """Verdict final du REPLAY après réponse précoce 'ussd_sent'.

        Reprend la boucle poll verify propre au replay (step4_poll_verify) hors
        de la requête /pay. Met à jour result en place (le caller settle + notifie).
        """
        spec = result.poll_after_close
        if not spec:
            return
        vp = spec["verify_params"]
        cfg = spec.get("cfg") or replay_flow.ReplayConfig.defaults()
        log.info("[REPLAY] finalize (post-ussd) — polling verify (flw_ref=%s)",
                 vp["flw_ref"])
        verify = await replay_flow.step4_poll_verify(
            vp["modalauditid"], vp["flw_ref"], cfg=cfg)

        # Le polling ne s'arrête que sur un verdict terminal (pas de timeout) :
        # interpret_verify le traduit ici. Fallback unknown si réponse inattendue.
        verdict = self.interpret_status("verify", verify, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
        else:
            result.final_status = verify.get("data", {}).get("status", "unknown")
            result.final_message = f"Statut Flutterwave: {result.final_status}"

        result.success = result.final_status == "successful"
        result.payment_status = result.final_status
        # Le verdict replay vient toujours du polling verify (cette boucle
        # n'écoute pas le registre webhook).
        if result.final_status in ("successful", "failed", "cancelled"):
            result.settled_by = "polling"
        # Validation USSD par le client (verify -> successful) : on horodate
        # l'instant pour la garde anti-doublon après paiement réussi.
        if result.final_status == "successful":
            import time as _t
            result.validated_at = _t.time()
        log.info("[REPLAY] verdict final (post-ussd): %s", result.final_status)


register(DigikuntzAggregator.name, DigikuntzAggregator)
