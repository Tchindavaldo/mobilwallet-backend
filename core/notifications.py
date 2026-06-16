"""Notifications du verdict final vers le client (webhook HTTP signé).

Quand une transaction settle sur un statut terminal, on POST le verdict au
callback_url de l'app, signé en HMAC (le client vérifie l'authenticité). L'envoi
est :
  - IDEMPOTENT : une réservation (transaction, event) dans webhook_deliveries
    garantit un seul envoi même si polling et webhook DigiKUNTZ settlent ensemble ;
  - NON BLOQUANT : déclenché en tâche de fond (asyncio.create_task) — ne retarde
    jamais la réponse /pay ni le 200 du webhook entrant ;
  - RÉESSAYÉ : backoff 0/5/30s, 3 tentatives, jusqu'à un 2xx.

L'émission temps réel (Socket.IO) se branche aussi ici en phase 3 (même payload).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

from core import tenants

log = logging.getLogger("ai_browser2")

_TERMINAL = {"successful", "failed", "cancelled", "expired"}
_RETRY_DELAYS = (0, 5, 30)  # secondes avant chaque tentative


def _sign(secret: str, ts: int, body: str) -> str:
    """Signature Stripe-like : HMAC_SHA256(secret, '<ts>.<body>')."""
    mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256)
    return mac.hexdigest()


def notify_settled(tx_id: int, result_or_status, *, transaction_ref: str = "",
                   amount: int = 0, network: str = "", phone: str = "",
                   end_user_ref: str | None = None,
                   provider_transaction_id: str = "",
                   callback_url: str = "") -> None:
    """Déclenche (fire-and-forget) la notification du verdict d'une transaction.

    `result_or_status` est soit un statut str, soit un objet portant .final_status
    et les champs métier. Non bloquant : programme une tâche de fond et rend la
    main immédiatement. No-op si le statut n'est pas terminal.
    """
    status = getattr(result_or_status, "final_status", None) or str(result_or_status)
    if status not in _TERMINAL:
        return
    payload_data = {
        "transaction_id": transaction_ref
        or getattr(result_or_status, "transaction_id", "") or "",
        "status": status,
        "amount": amount or getattr(result_or_status, "amount", 0),
        "network": network,
        "phone": phone,
        "end_user_ref": end_user_ref,
        "provider_transaction_id": provider_transaction_id
        or getattr(result_or_status, "provider_transaction_id", "") or "",
    }
    asyncio.create_task(_deliver(tx_id, status, payload_data, callback_url=callback_url))


async def _deliver(tx_id: int, status: str, data: dict, callback_url: str = "") -> None:
    """Corps de la tâche de fond : réserve (idempotence), signe, POST + retries."""
    event = f"transaction.{status}"

    app = await tenants.get_app_for_transaction(tx_id)
    if not app:
        return  # pas d'app rattachée -> rien à notifier

    # Réservation idempotente : si un autre chemin a déjà réservé, on s'abstient
    # (pour le webhook ET le push temps réel : un seul des deux chemins émet).
    if not await tenants.reserve_delivery(tx_id, app.get("app_id"), event):
        return

    data["app_id"] = app.get("app_id")
    ts = int(time.time())
    payload = {"id": f"evt_{tx_id}_{status}", "type": event, "created": ts, "data": data}

    # Push temps réel (Socket.IO) — best-effort, n'empêche pas le webhook HTTP.
    from core import realtime
    await realtime.emit_transaction_update(app.get("app_id"), payload)

    # Le webhook HTTP n'utilise QUE le callback_url fourni dans la requête /pay.
    # Aucun fallback (ni sur app.callback_url, ni sur une URL par défaut) : si le
    # client n'a pas demandé de webhook, aucun POST ne part. Le push Socket.IO,
    # lui, a déjà eu lieu ci-dessus (best-effort, pour qui écoute).
    webhook_url = callback_url or ""
    if not webhook_url:
        log.info("transaction.%s tx=%s : pas de webhook (aucun callback_url dans la requête)",
                 status, tx_id)
        await tenants.mark_delivery(tx_id, event, delivered=True, attempts=0)
        return
    body = json.dumps(payload, ensure_ascii=False)
    headers = {
        "Content-Type": "application/json",
    }

    attempts = 0
    last_error = ""
    async with httpx.AsyncClient(timeout=15) as client:
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                resp = await client.post(webhook_url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    await tenants.mark_delivery(tx_id, event, delivered=True, attempts=attempts)
                    log.info("webhook livré tx=%s event=%s (try %d)", tx_id, event, attempts)
                    return
                last_error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last_error = type(e).__name__
            log.warning("webhook échec tx=%s event=%s try=%d (%s)",
                        tx_id, event, attempts, last_error)

    await tenants.mark_delivery(tx_id, event, delivered=False,
                               attempts=attempts, error=last_error)
