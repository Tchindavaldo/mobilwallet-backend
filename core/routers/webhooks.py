"""Webhook entrant DigiKUNTZ (callback statut serveur-à-serveur)."""

import logging

from fastapi import APIRouter

from core.db import db
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
        # Débloque le polling en cours pour cette transaction (registre mémoire).
        status_poll.registry.deliver(
            provider_id, {"status": internal, "raw": raw, "data": payload.get("data")})
        # MAJ la ligne BD si on la retrouve (settle redondant mais idempotent).
        row = await db.get_transaction_by_provider_id(provider_id)
        if row:
            await db.update_status_by_provider_id(provider_id, internal,
                                                  message=f"Webhook DigiKUNTZ: {raw}")
            # Notifie l'app du verdict (idempotent : si le polling a déjà notifié
            # ce (tx, event), la réservation l'empêche d'envoyer en double).
            notify_settled(
                row["id"], internal, transaction_ref=row.get("transaction_ref", ""),
                amount=row.get("amount", 0), network=row.get("network", ""),
                phone=row.get("phone", ""), end_user_ref=row.get("end_user_ref"),
                provider_transaction_id=provider_id,
            )
    return {"received": True}
