"""Webhooks entrants des agrégateurs (callbacks statut serveur-à-serveur).

- POST /webhook/digikuntz
- POST /webhook/pawapay   (dépôts, payouts et remboursements pawaPay)
"""

import logging

from fastapi import APIRouter

from core import settlement
from core.db import db

log = logging.getLogger("ai_browser2")

router = APIRouter()


@router.post("/webhook/digikuntz", tags=["system"],
             summary="Webhook DigiKUNTZ (callback statut)", include_in_schema=False)
async def digikuntz_webhook(payload: dict):
    """Callback serveur DigiKUNTZ : POST {id, status, data} à chaque changement.

    On répond 200 TOUT DE SUITE (sinon DigiKUNTZ retry), puis on MAJ la BD et on
    dépose le verdict terminal dans le registre pour débloquer le polling en
    cours (le premier — webhook ou polling — qui a le terminal gagne, l'autre est
    idempotent). Identifié par l'id provider -> aucune collision en parallèle.
    Ne fonctionne que si ce backend a une URL publique joignable (cf.
    todo/webhook-digikuntz.md).
    """
    from aggregators.digikuntz import status_poll
    provider_id = str(payload.get("id", ""))
    raw = payload.get("status", "")
    internal = status_poll.STATUS_MAP.get(raw, raw or "unknown")
    log.info("🪝 Webhook DigiKUNTZ: id=%s status=%s -> %s", provider_id, raw, internal)

    if provider_id and internal in ("successful", "failed", "cancelled"):
        status_poll.registry.deliver(
            provider_id, {"status": internal, "raw": raw, "data": payload.get("data")})
        await settlement.settle_verdict(provider_id, internal, f"Webhook DigiKUNTZ: {raw}")
    return {"received": True}


@router.post("/webhook/pawapay", tags=["system"],
             summary="Webhook pawaPay (callback statut)", include_in_schema=False)
async def pawapay_webhook(payload: dict):
    """Callback serveur pawaPay : POST {depositId|payoutId|refundId, status, ...}.

    Une seule URL pour tous les types (déclarée dans le dashboard pawaPay). On
    identifie la transaction par l'id pawaPay (= provider_transaction_id), on
    réveille le polling en cours puis on applique le règlement commun.
    Répond toujours 200 pour éviter les retries inutiles.
    TODO : vérifier la signature (RFC-9421) si les callbacks signés sont activés.
    """
    from aggregators.pawapay import client
    if "refundId" in payload:
        # Remboursement : pas encore géré par la comptabilité -> trace seulement.
        log.info("🪝 Webhook pawaPay refund: id=%s status=%s",
                 payload.get("refundId"), payload.get("status"))
        return {"received": True}
    provider_id = str(payload.get("depositId") or payload.get("payoutId") or "")
    raw = str(payload.get("status", ""))
    failure = payload.get("failureReason")
    internal = client.map_status(raw, failure)
    log.info("🪝 Webhook pawaPay: id=%s status=%s -> %s", provider_id, raw, internal)
    payer = (payload.get("payer") or {}).get("accountDetails") or {}
    if payload.get("depositId") and payer:
        # Dépôt via page hébergée : réseau/numéro connus seulement maintenant.
        from aggregators.pawapay.operations import network_of
        await db.fill_payer(provider_id, network_of(payer.get("provider", "")),
                            payer.get("phoneNumber", ""))
    if provider_id and internal in client.TERMINAL:
        client.registry.deliver(
            provider_id, {"status": internal, "raw": raw, "failure": failure})
        if payload.get("payoutId"):
            from aggregators.pawapay.payout_flow import _message
            msg = _message(internal, failure)
        else:
            from aggregators.pawapay.aggregator import friendly_msg
            msg = friendly_msg(internal, failure, payload.get("provider") or
                               (payer or {}).get("provider"))
        signals = ({"failure_code": failure.get("failureCode"),
                    "failure_message": failure.get("failureMessage")} if failure else None)
        await settlement.settle_verdict(provider_id, internal, msg, error_signals=signals)
    return {"received": True}
