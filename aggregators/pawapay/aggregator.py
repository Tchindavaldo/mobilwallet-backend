"""PawapayAggregator — agrégateur « API directe » (aucun navigateur, aucun replay).

Payin : POST /v2/deposits -> l'opérateur envoie la demande PIN au client ->
statut final par callback (/webhook/pawapay) ou polling GET /v2/deposits/{id}.
/pay rend la main en `ussd_sent` puis finalise en tâche de fond via
`finalize_after_close` (même mécanique que DigiKUNTZ).

Les hooks navigateur/replay du contrat `Aggregator` n'ont pas de sens ici : ils
lèvent NotImplementedError. `/pay` détecte `direct_api = True` et appelle
`pay_direct` à la place.
"""

import logging
import time

from core import registry
from core.base import Aggregator, CurlTemplate, PaymentRequest, PaymentResult

from . import client

log = logging.getLogger("ai_browser2")

_MESSAGES = {
    "ussd_sent": "Demande de paiement envoyée. Validez-la sur votre téléphone avec votre code PIN.",
    "successful": "Paiement effectué avec succès.",
    "failed": "Le paiement a échoué.",
    "cancelled": "Paiement non validé / annulé.",
    "error": "Paiement introuvable chez le prestataire : rien n'a été débité. Merci de réessayer.",
}


def friendly_msg(status: str, failure: dict | None = None) -> str:
    """Message client. En échec/annulation, la raison précise (FR) prime."""
    detail = client.failure_message(failure)
    if detail and status in ("failed", "cancelled"):
        return detail
    return _MESSAGES.get(status, "Paiement en cours.")


class PawapayAggregator(Aggregator):
    name = "pawapay"
    direct_api = True
    supported_networks = ["MTN", "Orangemoney"]

    def network_label(self, network: str) -> str:
        return "Orange Money" if network == "Orangemoney" else "MTN Mobile Money"

    async def pay_direct(self, req: PaymentRequest, tx_id: int | None = None) -> PaymentResult:
        """Initie le dépôt pawaPay. Retour : ussd_sent (+ poll_after_close) ou verdict
        immédiat (REJECTED). Lève PawapayUpstreamError si l'API est injoignable."""
        result = PaymentResult()
        deposit_id = client.new_id()
        result.transaction_id = deposit_id
        result.provider_transaction_id = deposit_id
        # L'id provider doit être en BD AVANT l'appel : un callback rapide doit
        # pouvoir retrouver la ligne.
        if self.db is not None and tx_id is not None:
            await self.db.set_provider_id(tx_id, deposit_id)
        try:
            init = await client.initiate_deposit(
                deposit_id, req.amount, req.phone, req.network, req.raison,
                client_ref=str(tx_id or ""))
        except client.PawapayUpstreamError as e:
            result.error_code = e.code
            result.final_status = "error"
            return result
        result.payment_status = init["raw"]
        result.flutterwave_charge_body = str(init["body"])[:5000]
        result.flutterwave_charge_response = str(init["response"])[:5000]
        log.info("pawapay deposit %s: %s -> %s", deposit_id, init["raw"], init["internal"])

        if init["internal"] == "pending":
            result.final_status = "ussd_sent"
            result.final_message = friendly_msg("ussd_sent")
            result.ussd_sent_at = time.time()
            result.poll_after_close = {"kind": "deposits", "provider_id": deposit_id}
            return result
        # Refus à l'initiation : rien n'a atteint le client.
        result.final_status = init["internal"] if init["internal"] in client.TERMINAL else "failed"
        result.error = client.failure_message(init["failure"])
        result.final_message = friendly_msg(result.final_status, init["failure"])
        return result

    async def finalize_after_close(self, req: PaymentRequest, result: PaymentResult) -> None:
        """Attend le statut final (webhook ou polling) et le pose sur `result`."""
        spec = result.poll_after_close or {}
        verdict = await client.poll_until_final(spec.get("kind", "deposits"),
                                                spec["provider_id"])
        status = verdict["status"]
        result.final_status = status
        result.success = status == "successful"
        result.final_message = friendly_msg(status, verdict.get("failure"))
        result.settled_by = verdict.get("settled_by")
        result.poll_after_close = None
        if result.success:
            result.validated_at = time.time()

    # --- contrat Aggregator : hooks navigateur/replay sans objet pour pawaPay ---
    async def create_transaction(self, req: PaymentRequest) -> dict:
        raise NotImplementedError("pawaPay: utiliser pay_direct")

    def browser_objective(self, req: PaymentRequest) -> str:
        raise NotImplementedError("pawaPay: pas de mode navigateur")

    async def decide_browser_outcome(self, req, loop_result, result, session=None) -> None:
        raise NotImplementedError("pawaPay: pas de mode navigateur")

    def charge_request_matcher(self, r) -> bool:
        return False

    def verify_request_matcher(self, r) -> bool:
        return False

    def checkout_url_predicate(self, url: str) -> bool:
        return False

    def extract_curl_template(self, charge, verify, public_key) -> CurlTemplate | None:
        return None

    async def replay(self, req: PaymentRequest, template: CurlTemplate) -> PaymentResult:
        raise NotImplementedError("pawaPay: pas de mode replay")

    async def pay_via_browser(self, req: PaymentRequest, tx_id: int | None = None) -> PaymentResult:
        raise NotImplementedError("pawaPay: pas de mode navigateur")

    def interpret_status(self, stage: str, resp: dict, network: str):
        return None


registry.register("pawapay", PawapayAggregator)
