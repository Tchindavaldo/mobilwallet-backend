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
                   provider_transaction_id: str = "") -> None:
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
    asyncio.create_task(_deliver(tx_id, status, payload_data))


async def _deliver(tx_id: int, status: str, data: dict) -> None:
    """Corps de la tâche de fond : réserve (idempotence), signe, POST + retries."""
    event = f"transaction.{status}"

    app = await tenants.get_app_for_transaction(tx_id)
    if not app or not app.get("callback_url"):
        return  # pas d'app ou pas de callback configuré -> rien à notifier

    # Réservation idempotente : si un autre chemin a déjà réservé, on s'abstient.
    if not await tenants.reserve_delivery(tx_id, app.get("app_id"), event):
        return

    data["app_id"] = app.get("app_id")
    ts = int(time.time())
    payload = {"id": f"evt_{tx_id}_{status}", "type": event, "created": ts, "data": data}
    body = json.dumps(payload, ensure_ascii=False)
    signature = _sign(app.get("webhook_secret") or "", ts, body)
    headers = {
        "Content-Type": "application/json",
        "X-MobileWallet-Signature": f"t={ts},v1={signature}",
    }

    attempts = 0
    last_error = ""
    async with httpx.AsyncClient(timeout=15) as client:
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                resp = await client.post(app["callback_url"], content=body, headers=headers)
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
