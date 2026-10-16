"""Opérations pawaPay hors paiement unitaire, appelées via core/aggregator_ops.py :
prédiction d'opérateur, renvoi de callback, annulation de payout en file,
payouts groupés (bulk) et page de paiement hébergée."""

import logging

from . import client
from .payout_flow import friendly_msg

log = logging.getLogger("ai_browser2")

# Code provider pawaPay -> réseau canonique MobileWallet.
_NETWORKS = {"MTN_MOMO_CMR": "MTN", "ORANGE_CMR": "Orangemoney"}
_KINDS = {"payin": "deposits", "payout": "payouts"}

# Doc pawaPay : page abandonnée -> dépôt NOT_FOUND, à considérer FAILED après 15 min.
PAGE_EXPIRY_S = 15 * 60


def network_of(provider_code: str) -> str:
    return _NETWORKS.get(provider_code or "", provider_code or "")


async def predict_network(phone: str) -> dict:
    """{phone (format international), network, provider, country}."""
    r = await client.predict_provider(phone)
    return {"phone": r.get("phoneNumber"), "network": network_of(r.get("provider")),
            "provider": r.get("provider"), "country": r.get("country"),
            "failure": r.get("failureReason")}


async def resend_callback(kind: str, provider_id: str) -> dict:
    """Demande à pawaPay de renvoyer le webhook final -> {accepted, raw}."""
    r = await client.resend_callback(_KINDS.get(kind, "deposits"), provider_id)
    return {"accepted": r.get("status") == "ACCEPTED", "raw": r.get("status"),
            "failure": r.get("failureReason")}


async def cancel_payout(provider_id: str) -> dict:
    """Annule un payout encore ENQUEUED -> {accepted, raw}. Le verdict
    (cancelled) arrive ensuite par webhook, qui rembourse le solde."""
    r = await client.fail_enqueued_payout(provider_id)
    return {"accepted": r.get("status") == "ACCEPTED", "raw": r.get("status"),
            "failure": r.get("failureReason")}


async def initiate_payouts_bulk(reqs: list) -> list[dict]:
    """POST /v2/payouts/bulk pour des PayoutRequest. Retourne, dans l'ordre,
    le même format que payout_flow.initiate_payout. Lève client.PawapayUpstreamError."""
    bodies = [client.payout_body(client.new_id(), r.amount, r.account_number,
                                 r.account_bank_code, r.narration or r.receiver_name)
              for r in reqs]
    res = await client.initiate_payouts_bulk(bodies)
    out = []
    for body, r in zip(bodies, res):
        log.info("pawapay bulk payout %s: %s -> %s", r["payout_id"], r["raw"], r["internal"])
        out.append({"provider_id": r["payout_id"], "transaction_ref": r["payout_id"],
                    "raw_status": r["raw"], "internal_status": r["internal"],
                    "message": friendly_msg(r["internal"]),
                    "data": r["failure"] or {}, "body": body})
    return out


async def create_payment_page(amount: int, label: str, return_url: str,
                              phone: str = "", language: str = "FR") -> dict:
    """POST /v2/paymentpage -> {provider_id, redirect_url}. Le client paie sur la
    page pawaPay ; le verdict arrive par webhook comme un dépôt classique."""
    deposit_id = client.new_id()
    body = {
        "depositId": deposit_id, "returnUrl": return_url,
        "customerMessage": client.customer_message(label),
        "amountDetails": {"amount": str(int(amount)), "currency": client._pp.currency},
        "country": client._pp.country, "language": language, "reason": label[:50],
    }
    if phone:
        body["phoneNumber"] = client.msisdn(phone)
    r = await client._post("/v2/paymentpage", body)
    return {"provider_id": deposit_id, "redirect_url": r.get("redirectUrl"),
            "failure": r.get("failureReason"), "raw": r}


async def ussd_code(network: str) -> str | None:
    """Code USSD de secours (valider le PIN si la demande n'arrive pas)."""
    from aggregators.pawapay import account_conf
    from core.fees import fee_network
    return await account_conf.ussd_code(fee_network(network), "DEPOSIT")
