"""Payout (virement sortant) pawaPay — même interface que digikuntz.payout_flow.

`core/routers/payout.py` choisit le module selon l'agrégateur et appelle
`initiate_payout` / `poll_payout_status` / `check_payout_status` / `friendly_msg`
/ `PayoutUpstreamError`.

Pas de polling continu sur les payouts pawaPay : le verdict arrive par webhook.
`check_payout_status` fait UN appel GET /v2/payouts/{id}, à la demande
(endpoint `GET /payout/{ref}/check`).
"""

import logging

from . import client

log = logging.getLogger("ai_browser2")

PayoutUpstreamError = client.PawapayUpstreamError


def friendly_msg(status: str) -> str:
    if status == "successful":
        return "Virement effectué avec succès."
    if status == "cancelled":
        return "Virement annulé (non abouti)."
    if status == "failed":
        return "Échec du virement."
    if status == "error":
        return "Virement introuvable chez le prestataire : rien n'a été envoyé."
    return "Virement en cours."


async def initiate_payout(req) -> dict:
    """POST /v2/payouts. `req` = PayoutRequest (account_bank_code MTN|ORANGEMONEY).

    Retourne {provider_id, transaction_ref, raw_status, internal_status, data, body}.
    Lève PayoutUpstreamError si l'API pawaPay est injoignable."""
    payout_id = client.new_id()
    init = await client.initiate_payout(
        payout_id, req.amount, req.account_number, req.account_bank_code,
        req.narration or req.receiver_name)
    log.info("pawapay payout %s: %s -> %s", payout_id, init["raw"], init["internal"])
    return {
        "provider_id": payout_id,
        "transaction_ref": payout_id,
        "raw_status": init["raw"],
        "internal_status": init["internal"],
        "data": init["failure"] or {},
        "body": init["body"],
    }


def _message(status: str, failure: dict | None) -> str:
    msg = friendly_msg(status)
    detail = client.failure_message(failure)
    return f"{msg} {detail}" if detail and status in ("failed", "cancelled") else msg


async def poll_payout_status(provider_id: str) -> dict:
    """Attend le statut final livré par le webhook (aucun appel à pawaPay)."""
    v = await client.wait_webhook(provider_id)
    return {"status": v["status"], "message": _message(v["status"], v.get("failure")),
            "settled_by": v["settled_by"]}


async def check_payout_status(provider_id: str) -> dict | None:
    """UN appel GET /v2/payouts/{id}. Retourne {status, raw, message, terminal}
    ou None si pawaPay est injoignable. Un statut terminal est déposé dans le
    registre (réveille l'attente webhook, qui règle le payout)."""
    res = await client.fetch_status("payouts", provider_id)
    if res is None:
        return None
    status = res["internal"]
    terminal = status in client.TERMINAL
    if terminal:
        client.registry.deliver(provider_id, {"status": status, "raw": res["raw"],
                                              "failure": res["failure"],
                                              "settled_by": "manual_check"})
    return {"status": status, "raw": res["raw"], "terminal": terminal,
            "message": _message(status, res["failure"])}
