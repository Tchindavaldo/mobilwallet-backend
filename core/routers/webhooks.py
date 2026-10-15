"""Webhooks entrants des agrégateurs (callbacks statut serveur-à-serveur).

- POST /webhook/digikuntz
- POST /webhook/pawapay   (dépôts, payouts et remboursements pawaPay)
"""

import logging

from fastapi import APIRouter

from core.db import db
from core.fees import compute_fees
from core.notifications import notify_settled

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
        await _settle(provider_id, internal, f"Webhook DigiKUNTZ: {raw}")
    return {"received": True}


async def _settle(provider_id: str, internal: str, message: str) -> None:
    """MAJ BD + comptabilité + notification client pour un verdict terminal
    reçu d'un agrégateur. Commun à tous les webhooks ; idempotent."""
    # MAJ la ligne BD si on la retrouve (settle redondant mais idempotent).
    row = await db.get_transaction_by_provider_id(provider_id)
    if row:
        await db.update_status_by_provider_id(provider_id, internal,
                                              message=message)
        # Comptabilité (idempotente, via ledger) :
        #   - un PAYIN réussi crédite le solde de l'app ;
        #   - un PAYOUT échoué/annulé rembourse le débit réservé à l'initiation.
        # (Un payout réussi ne fait rien : le débit reste.)
        row_type = (row.get("type") or "payin").lower()
        app_id = row.get("app_id")
        if app_id and row.get("id"):
            if row_type == "payin" and internal == "successful":
                # Split : commission MW (défaut agrégateur ou surcharge app) → plateforme, reste → app.
                # Les frais DigiKUNTZ sont pris directement sur le client (hors comptabilité).
                agg_config = await db.get_aggregator_config(row.get("aggregator", ""))
                app_cfg = await db.get_app_commission(app_id)
                breakdown = compute_fees(row.get("amount", 0), agg_config, app_cfg)
                await db.credit_app(app_id, breakdown.app_amount,
                                    transaction_id=row["id"],
                                    reason="payin_successful")
                if breakdown.platform_amount > 0:
                    await db.credit_platform(breakdown.platform_amount,
                                              transaction_id=row["id"],
                                              reason="payin_fees")
            elif row_type == "payout" and internal in ("failed", "cancelled"):
                await db.credit_app(app_id, row.get("amount", 0),
                                    transaction_id=row["id"],
                                    reason="payout_refund")
        # Notifie l'app du verdict (idempotent : si le polling a déjà notifié
        # ce (tx, event), la réservation l'empêche d'envoyer en double).
        notify_settled(
            row["id"], internal, transaction_ref=row.get("transaction_ref", ""),
            amount=row.get("amount", 0), network=row.get("network", ""),
            phone=row.get("phone", ""), end_user_ref=row.get("end_user_ref"),
            provider_transaction_id=provider_id,
        )


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
    if provider_id and internal in client.TERMINAL:
        client.registry.deliver(
            provider_id, {"status": internal, "raw": raw, "failure": failure})
        await _settle(provider_id, internal, f"Webhook pawaPay: {raw}")
    return {"received": True}
