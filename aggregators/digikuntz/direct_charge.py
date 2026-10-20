"""Charge directe DigiKUNTZ (mode `direct` de /pay) : UN appel
`POST {base}/direct-charge` (auth x-user-id / x-secret-key) qui pousse l'USSD
sur le téléphone du payeur, sans page Flutterwave ni navigateur. Le verdict
arrive ensuite par webhook ou polling (status_poll.poll_transaction).
Complète le chemin navigateur, ne le remplace pas."""

import logging

import httpx

from core.config import settings
from core.upstream_errors import classify_upstream_error

from . import status_poll

log = logging.getLogger("ai_browser2")

_dk = settings.digikuntz


class DirectChargeUpstreamError(Exception):
    """Panne amont (API DigiKUNTZ injoignable / 5xx) à l'initiation."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def initiate(req) -> dict:
    """POST {base}/direct-charge. `req` = PaymentRequest (network = code
    paymentMethodCode : MTN / ORANGEMONEY). Retourne {provider_id, transaction_ref,
    raw_status, internal_status, body, response} ; un refus 4xx remonte avec
    internal_status='failed' et le message de DigiKUNTZ."""
    phone = req.phone.replace("+", "")
    body = {
        "estimation": req.amount,
        "userPhone": phone if phone.startswith("237") else f"237{phone}",
        "userEmail": req.email,
        "userCountry": "Cameroon",
        "senderName": req.sender_name,
        "paymentMethodCode": req.network,
        "raisonForTransfer": req.raison,
    }
    if _dk.use_callback and _dk.callback_url:
        body["callbackUrl"] = _dk.callback_url
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_dk.base}/direct-charge", json=body,
                                     headers=status_poll._headers())
    except httpx.HTTPError as e:
        raise DirectChargeUpstreamError(classify_upstream_error(e) or "network_unavailable") from e
    if resp.status_code >= 500:
        raise DirectChargeUpstreamError(
            classify_upstream_error(httpx.HTTPStatusError(
                "5xx", request=resp.request, response=resp)) or "network_unavailable")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        log.warning("direct-charge refusée (%s): %s", resp.status_code, payload)
        return {"provider_id": "", "transaction_ref": "", "raw_status": str(resp.status_code),
                "internal_status": "failed", "body": body, "response": payload,
                "message": payload.get("message") if isinstance(payload, dict) else None}
    raw = payload.get("status", "")
    data = payload.get("data") or {}
    return {
        "provider_id": str(payload.get("id", "")),
        "transaction_ref": data.get("transactionRef", ""),
        "raw_status": raw,
        "internal_status": status_poll.STATUS_MAP.get(raw, raw or "unknown"),
        "body": body,
        "response": payload,
    }
