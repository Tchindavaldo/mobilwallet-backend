"""Payout (virement sortant / retrait) DigiKUNTZ.

Bien plus simple qu'un payin : un SEUL appel HTTP `POST {base}/payout` (auth
x-user-id / x-secret-key), qui renvoie immédiatement `payout_pending`, puis dont
le statut évolue (`payout_success` / `payout_closed` / `payout_error`). Pas de
navigateur, pas de LLM, pas de Flutterwave, pas de replay.

Le suivi du verdict final réutilise le MÊME mécanisme que le payin :
  - `poll_payout_status` interroge `GET {base}/transaction` (mapping payout_* dans
    status_poll.STATUS_MAP) jusqu'à un statut terminal ;
  - le registre webhook⇄polling de status_poll coordonne polling et webhook entrant
    (le premier au terminal gagne, l'autre est idempotent).

Philosophie CLAUDE.md : les statuts payout_* sont des FAITS opérateur mécaniques
(statut renvoyé par DigiKUNTZ), pas un verdict déduit par le code — le mapping
direct est l'exception explicitement autorisée. Aucune IA n'intervient ici.
"""

import asyncio
import logging
import time

import httpx

from core.config import settings
from core.upstream_errors import NETWORK_UNAVAILABLE, classify_upstream_error

from . import status_poll

log = logging.getLogger("ai_browser2")

_dk = settings.digikuntz
_TERMINAL = {"successful", "failed", "cancelled"}


class PayoutUpstreamError(Exception):
    """Panne amont (API DigiKUNTZ indisponible) lors de l'initiation du payout.

    Porte un code machine (`network_unavailable`) pour que le router renvoie un
    503 propre, comme le fait /pay pour les pannes amont."""

    def __init__(self, code: str = NETWORK_UNAVAILABLE):
        self.code = code
        super().__init__(code)


async def initiate_payout(req) -> dict:
    """Lance le virement : POST {base}/payout. `req` = PayoutRequest.

    Retourne {provider_id, transaction_ref, raw_status, internal_status, data, body}.
    Lève PayoutUpstreamError si l'API DigiKUNTZ est indisponible (5xx/réseau).
    """
    body = {
        "amount": req.amount,
        "accountBankCode": req.account_bank_code,
        "accountNumber": req.account_number,
        "receiverName": req.receiver_name,
        "currency": req.currency,
        "narration": req.narration,
    }
    url = f"{_dk.base}/payout"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=status_poll._headers())
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        code = classify_upstream_error(e)
        if code:
            log.warning("initiate_payout: panne amont (%s)", type(e).__name__)
            raise PayoutUpstreamError(code) from e
        # Réponse exploitable mais non-5xx (ex. 400 refus) : on remonte tel quel.
        log.warning("initiate_payout: échec non-amont (%s)", type(e).__name__)
        raise

    raw = payload.get("status", "")
    data = payload.get("data") or {}
    return {
        "provider_id": str(payload.get("id", "")),
        "transaction_ref": data.get("transactionRef", ""),
        "raw_status": raw,
        "internal_status": status_poll.STATUS_MAP.get(raw, raw or "unknown"),
        "data": data,
        "body": body,
    }


def friendly_msg(status: str) -> str:
    """Message FR court selon le statut terminal d'un payout."""
    if status == "successful":
        return "Virement effectué avec succès."
    if status == "cancelled":
        return "Virement annulé / fermé (non abouti)."
    if status == "failed":
        return "Échec du virement."
    return "Virement en cours."


async def poll_payout_status(provider_id: str) -> dict:
    """Poll GET {base}/transaction jusqu'à un statut terminal pour ce payout.

    Calqué sur status_poll.poll_verify_flutterwave : écoute AUSSI le registre
    webhook⇄polling (le premier au terminal gagne), SANS TIMEOUT (l'opérateur
    finit toujours par trancher). Retourne {status, message, settled_by}.
    """
    ev = status_poll.registry.register(provider_id) if provider_id else None
    log.info("poll_payout_status: provider_id=%s (sans timeout)", provider_id)
    start = time.monotonic()
    i = 0
    try:
        while True:
            # (a) le webhook a-t-il déjà livré le verdict ? -> on s'arrête net.
            if ev is not None and ev.is_set():
                v = status_poll.registry.take_verdict(provider_id)
                if v and v.get("status") in _TERMINAL:
                    log.info("poll_payout_status: verdict WEBHOOK %s", v["status"])
                    return {"status": v["status"],
                            "message": friendly_msg(v["status"]),
                            "settled_by": "webhook"}
            i += 1
            res = await status_poll.fetch_status(provider_id)
            if res is not None:
                internal = res["internal"]
                elapsed = int(time.monotonic() - start)
                log.info("payout poll %d (t+%ds): raw=%s -> %s",
                         i, elapsed, res["raw"], internal)
                if internal in _TERMINAL:
                    # On a le verdict en premier : on le dépose pour un webhook
                    # tardif (idempotence) et on conclut.
                    if provider_id:
                        status_poll.registry.deliver(
                            provider_id,
                            {"status": internal, "raw": res["raw"], "data": res.get("data")})
                    return {"status": internal, "message": friendly_msg(internal),
                            "settled_by": "polling"}
            # Attente du prochain tick, interrompue tôt si le webhook livre.
            if ev is not None:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(2)
    finally:
        if provider_id:
            status_poll.registry.close(provider_id)
