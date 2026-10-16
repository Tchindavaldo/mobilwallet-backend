"""Payout (virement sortant) pawaPay — même interface que digikuntz.payout_flow.

`core/routers/payout.py` choisit le module selon l'agrégateur et appelle
`initiate_payout` / `poll_payout_status` / `friendly_msg` / `PayoutUpstreamError`.
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


async def poll_payout_status(provider_id: str) -> dict:
    """Attend le statut final (webhook ou polling GET /v2/payouts/{id})."""
    v = await client.poll_until_final("payouts", provider_id)
    msg = friendly_msg(v["status"])
    detail = client.failure_message(v.get("failure"))
    if detail and v["status"] in ("failed", "cancelled"):
        msg = f"{msg} {detail}"
    return {"status": v["status"], "message": msg, "settled_by": v["settled_by"]}
